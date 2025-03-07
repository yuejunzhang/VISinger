import copy
import math
import torch
from torch import nn
from torch.nn import functional as F

import commons
import modules
import attentions

from torch.nn import Conv1d, ConvTranspose1d, AvgPool1d, Conv2d
from torch.nn.utils import weight_norm, remove_weight_norm, spectral_norm
from commons import init_weights, get_padding


class DurationPredictor(nn.Module):
    def __init__(
        self, in_channels, filter_channels, kernel_size, p_dropout, gin_channels=0
    ):
        super().__init__()

        self.in_channels = in_channels + 1
        self.filter_channels = filter_channels
        self.kernel_size = kernel_size
        self.p_dropout = p_dropout
        self.gin_channels = gin_channels

        self.drop = nn.Dropout(p_dropout)
        self.conv_1 = nn.Conv1d(
            in_channels + 1, filter_channels, kernel_size, padding=kernel_size // 2
        )
        self.norm_1 = modules.LayerNorm(filter_channels)
        self.conv_2 = nn.Conv1d(
            filter_channels, filter_channels, kernel_size, padding=kernel_size // 2
        )
        self.norm_2 = modules.LayerNorm(filter_channels)
        self.proj = nn.Conv1d(filter_channels, 1, 1)

        if gin_channels != 0:
            self.cond = nn.Conv1d(gin_channels, in_channels, 1)

    def forward(self, x, x_mask, score_dur, g=None):
        x = torch.detach(x)
        score_dur = torch.detach(score_dur)
        score_dur = score_dur.unsqueeze(1)
        x = torch.cat((x, score_dur), 1)
        if g is not None:
            g = torch.detach(g)
            x = x + self.cond(g)
        x = self.conv_1(x * x_mask)
        x = torch.relu(x)
        x = self.norm_1(x)
        x = self.drop(x)
        x = self.conv_2(x * x_mask)
        x = torch.relu(x)
        x = self.norm_2(x)
        x = self.drop(x)
        x = self.proj(x * x_mask)
        return x * x_mask


class SinusoidalPositionalEmbedding(nn.Module):
    """This module produces sinusoidal positional embeddings of any length.

    Padding symbols are ignored.
    """

    def __init__(self, embedding_dim, padding_idx, init_size=5000):
        super().__init__()
        self.embedding_dim = embedding_dim
        self.padding_idx = padding_idx
        self.weights = SinusoidalPositionalEmbedding.get_embedding(
            init_size,
            embedding_dim,
            padding_idx,
        )
        self.register_buffer("_float_tensor", torch.FloatTensor(1))

    @staticmethod
    def get_embedding(num_embeddings, embedding_dim, padding_idx=None):
        """Build sinusoidal embeddings.

        This matches the implementation in tensor2tensor, but differs slightly
        from the description in Section 3.5 of "Attention Is All You Need".
        """
        half_dim = embedding_dim // 2
        emb = math.log(10000) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, dtype=torch.float) * -emb)
        emb = torch.arange(num_embeddings, dtype=torch.float).unsqueeze(
            1
        ) * emb.unsqueeze(0)
        emb = torch.cat([torch.sin(emb), torch.cos(emb)], dim=1).view(
            num_embeddings, -1
        )
        if embedding_dim % 2 == 1:
            # zero pad
            emb = torch.cat([emb, torch.zeros(num_embeddings, 1)], dim=1)
        if padding_idx is not None:
            emb[padding_idx, :] = 0
        return emb

    def make_positions(tensor, padding_idx):
        """Replace non-padding symbols with their position numbers.

        Position numbers begin at padding_idx+1. Padding symbols are ignored.
        """
        # The series of casts and type-conversions here are carefully
        # balanced to both work with ONNX export and XLA. In particular XLA
        # prefers ints, cumsum defaults to output longs, and ONNX doesn't know
        # how to handle the dtype kwarg in cumsum.
        mask = tensor.ne(padding_idx).int()
        return (torch.cumsum(mask, dim=1).type_as(mask) * mask).long() + padding_idx

    def forward(self, bsz, seq_len, input):
        """Input is expected to be of size [bsz seqlen]."""
        max_pos = self.padding_idx + 1 + seq_len
        if self.weights is None or max_pos > self.weights.size(0):
            # recompute/expand embeddings if needed
            self.weights = SinusoidalPositionalEmbedding.get_embedding(
                max_pos,
                self.embedding_dim,
                self.padding_idx,
            )
        self.weights = self.weights.to(self._float_tensor)
        positions = SinusoidalPositionalEmbedding.make_positions(
            input, self.padding_idx
        )
        return (
            self.weights.index_select(0, positions.view(-1))
            .view(bsz, seq_len, -1)
            .detach()
        )


class TextEncoder(nn.Module):
    def __init__(
        self,
        n_vocab,
        out_channels,
        hidden_channels,
        filter_channels,
        n_heads,
        n_layers,
        kernel_size,
        p_dropout,
    ):
        super().__init__()
        self.out_channels = out_channels
        self.hidden_channels = hidden_channels
        self.filter_channels = filter_channels
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.kernel_size = kernel_size
        self.p_dropout = p_dropout

        self.emb_phone = nn.Embedding(n_vocab, hidden_channels)  # phone lables
        self.emb_score = nn.Embedding(128, hidden_channels)  # pitch notes
        self.emb_score_dur = nn.Embedding(600, hidden_channels)  # 512
        self.emb_slurs = nn.Embedding(2, hidden_channels)  # phone slur
        nn.init.normal_(self.emb_phone.weight, 0.0, hidden_channels**-0.5)
        nn.init.normal_(self.emb_score.weight, 0.0, hidden_channels**-0.5)
        nn.init.normal_(self.emb_score_dur.weight, 0.0, hidden_channels**-0.5)
        nn.init.normal_(self.emb_slurs.weight, 0.0, hidden_channels**-0.5)

        self.encoder = attentions.Encoder(
            hidden_channels, filter_channels, n_heads, n_layers, kernel_size, p_dropout
        )

        # max mel = 4000, 64seconds
        self.embed_positions = SinusoidalPositionalEmbedding(
            hidden_channels,
            padding_idx=0,
            init_size=4000,
        )
        self.drop = nn.Dropout(p_dropout)
        # self.proj = nn.Conv1d(hidden_channels, out_channels * 2, 1)

    def forward(self, phone, score, score_dur, slurs, lengths):
        x = self.emb_phone(phone)

        x = x + self.emb_score(score)
        x = x + self.emb_score_dur(score_dur)
        x = x + self.emb_slurs(slurs)

        x = x * math.sqrt(self.hidden_channels)  # [b, t, h]
        x = torch.transpose(x, 1, -1)  # [b, h, t]
        x_mask = torch.unsqueeze(commons.sequence_mask(lengths, x.size(2)), 1).to(
            x.dtype
        )
        # print("phone", phone)
        # print("score", score)
        # print("score_dur", score_dur)
        # print("slurs", slurs)
        # print("lengths", lengths)

        x = self.encoder(x * x_mask, x_mask)

        batch_size = x.shape[0]
        seque_size = x.shape[-1]

        pos_in = torch.transpose(x, 1, -1)[..., 0]
        p = self.embed_positions(batch_size, seque_size, pos_in)
        p = torch.transpose(p, 1, -1)  # [b, h, len]

        x = x + p
        x = self.drop(x)

        # stats = self.proj(x) * x_mask

        # m, logs = torch.split(stats, self.out_channels, dim=1)
        # return x, m, logs, x_mask
        return x, x_mask


class ResidualCouplingBlock(nn.Module):
    def __init__(
        self,
        channels,
        hidden_channels,
        kernel_size,
        dilation_rate,
        n_layers,
        n_flows=4,
        gin_channels=0,
    ):
        super().__init__()
        self.channels = channels
        self.hidden_channels = hidden_channels
        self.kernel_size = kernel_size
        self.dilation_rate = dilation_rate
        self.n_layers = n_layers
        self.n_flows = n_flows
        self.gin_channels = gin_channels

        self.flows = nn.ModuleList()
        for i in range(n_flows):
            self.flows.append(
                modules.ResidualCouplingLayer(
                    channels,
                    hidden_channels,
                    kernel_size,
                    dilation_rate,
                    n_layers,
                    gin_channels=gin_channels,
                    mean_only=True,
                )
            )
            self.flows.append(modules.Flip())

    def forward(self, x, x_mask, g=None, reverse=False):
        if not reverse:
            for flow in self.flows:
                x, _ = flow(x, x_mask, g=g, reverse=reverse)
        else:
            for flow in reversed(self.flows):
                x = flow(x, x_mask, g=g, reverse=reverse)
        return x

    def remove_weight_norm(self):
        for i in range(self.n_flows):
            self.flows[i * 2].remove_weight_norm()


class PosteriorEncoder(nn.Module):
    def __init__(
        self,
        in_channels,
        out_channels,
        hidden_channels,
        kernel_size,
        dilation_rate,
        n_layers,
        gin_channels=0,
    ):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.hidden_channels = hidden_channels
        self.kernel_size = kernel_size
        self.dilation_rate = dilation_rate
        self.n_layers = n_layers
        self.gin_channels = gin_channels

        self.pre = nn.Conv1d(in_channels, hidden_channels, 1)
        self.enc = modules.WN(
            hidden_channels,
            kernel_size,
            dilation_rate,
            n_layers,
            gin_channels=gin_channels,
        )
        self.proj = nn.Conv1d(hidden_channels, out_channels * 2, 1)

    def forward(self, x, x_lengths, g=None):
        x_mask = torch.unsqueeze(commons.sequence_mask(x_lengths, x.size(2)), 1).to(
            x.dtype
        )
        x = self.pre(x) * x_mask
        x = self.enc(x, x_mask, g=g)
        stats = self.proj(x) * x_mask
        m, logs = torch.split(stats, self.out_channels, dim=1)
        z = (m + torch.randn_like(m) * torch.exp(logs)) * x_mask
        return z, m, logs, x_mask

    def remove_weight_norm(self):
        self.enc.remove_weight_norm()


class Generator(torch.nn.Module):
    def __init__(
        self,
        initial_channel,
        resblock,
        resblock_kernel_sizes,
        resblock_dilation_sizes,
        upsample_rates,
        upsample_initial_channel,
        upsample_kernel_sizes,
        gin_channels=0,
    ):
        super(Generator, self).__init__()
        self.num_kernels = len(resblock_kernel_sizes)
        self.num_upsamples = len(upsample_rates)
        self.conv_pre = Conv1d(
            initial_channel, upsample_initial_channel, 7, 1, padding=3
        )
        resblock = modules.ResBlock1 if resblock == "1" else modules.ResBlock2

        self.ups = nn.ModuleList()
        for i, (u, k) in enumerate(zip(upsample_rates, upsample_kernel_sizes)):
            self.ups.append(
                weight_norm(
                    ConvTranspose1d(
                        upsample_initial_channel // (2**i),
                        upsample_initial_channel // (2 ** (i + 1)),
                        k,
                        u,
                        padding=(k - u) // 2,
                    )
                )
            )

        self.resblocks = nn.ModuleList()
        for i in range(len(self.ups)):
            ch = upsample_initial_channel // (2 ** (i + 1))
            for j, (k, d) in enumerate(
                zip(resblock_kernel_sizes, resblock_dilation_sizes)
            ):
                self.resblocks.append(resblock(ch, k, d))

        self.conv_post = Conv1d(ch, 1, 7, 1, padding=3, bias=False)
        self.ups.apply(init_weights)

        if gin_channels != 0:
            self.cond = nn.Conv1d(gin_channels, upsample_initial_channel, 1)

    def forward(self, x, g=None):
        x = self.conv_pre(x)
        if g is not None:
            x = x + self.cond(g)

        for i in range(self.num_upsamples):
            x = F.leaky_relu(x, modules.LRELU_SLOPE)
            x = self.ups[i](x)
            xs = None
            for j in range(self.num_kernels):
                if xs is None:
                    xs = self.resblocks[i * self.num_kernels + j](x)
                else:
                    xs += self.resblocks[i * self.num_kernels + j](x)
            x = xs / self.num_kernels
        x = F.leaky_relu(x)
        x = self.conv_post(x)
        x = torch.tanh(x)

        return x

    def remove_weight_norm(self):
        for l in self.ups:
            remove_weight_norm(l)
        for l in self.resblocks:
            l.remove_weight_norm()


class DiscriminatorP(torch.nn.Module):
    def __init__(self, period, kernel_size=5, stride=3, use_spectral_norm=False):
        super(DiscriminatorP, self).__init__()
        self.period = period
        self.use_spectral_norm = use_spectral_norm
        norm_f = weight_norm if use_spectral_norm == False else spectral_norm
        self.convs = nn.ModuleList(
            [
                norm_f(
                    Conv2d(
                        1,
                        32,
                        (kernel_size, 1),
                        (stride, 1),
                        padding=(get_padding(kernel_size, 1), 0),
                    )
                ),
                norm_f(
                    Conv2d(
                        32,
                        128,
                        (kernel_size, 1),
                        (stride, 1),
                        padding=(get_padding(kernel_size, 1), 0),
                    )
                ),
                norm_f(
                    Conv2d(
                        128,
                        512,
                        (kernel_size, 1),
                        (stride, 1),
                        padding=(get_padding(kernel_size, 1), 0),
                    )
                ),
                norm_f(
                    Conv2d(
                        512,
                        1024,
                        (kernel_size, 1),
                        (stride, 1),
                        padding=(get_padding(kernel_size, 1), 0),
                    )
                ),
                norm_f(
                    Conv2d(
                        1024,
                        1024,
                        (kernel_size, 1),
                        1,
                        padding=(get_padding(kernel_size, 1), 0),
                    )
                ),
            ]
        )
        self.conv_post = norm_f(Conv2d(1024, 1, (3, 1), 1, padding=(1, 0)))

    def forward(self, x):
        fmap = []

        # 1d to 2d
        b, c, t = x.shape
        if t % self.period != 0:  # pad first
            n_pad = self.period - (t % self.period)
            x = F.pad(x, (0, n_pad), "reflect")
            t = t + n_pad
        x = x.view(b, c, t // self.period, self.period)

        for l in self.convs:
            x = l(x)
            x = F.leaky_relu(x, modules.LRELU_SLOPE)
            fmap.append(x)
        x = self.conv_post(x)
        fmap.append(x)
        x = torch.flatten(x, 1, -1)

        return x, fmap


class DiscriminatorS(torch.nn.Module):
    def __init__(self, use_spectral_norm=False):
        super(DiscriminatorS, self).__init__()
        norm_f = weight_norm if use_spectral_norm == False else spectral_norm
        self.convs = nn.ModuleList(
            [
                norm_f(Conv1d(1, 16, 15, 1, padding=7)),
                norm_f(Conv1d(16, 64, 41, 4, groups=4, padding=20)),
                norm_f(Conv1d(64, 256, 41, 4, groups=16, padding=20)),
                norm_f(Conv1d(256, 1024, 41, 4, groups=64, padding=20)),
                norm_f(Conv1d(1024, 1024, 41, 4, groups=256, padding=20)),
                norm_f(Conv1d(1024, 1024, 5, 1, padding=2)),
            ]
        )
        self.conv_post = norm_f(Conv1d(1024, 1, 3, 1, padding=1))

    def forward(self, x):
        fmap = []

        for l in self.convs:
            x = l(x)
            x = F.leaky_relu(x, modules.LRELU_SLOPE)
            fmap.append(x)
        x = self.conv_post(x)
        fmap.append(x)
        x = torch.flatten(x, 1, -1)

        return x, fmap


class MultiPeriodDiscriminator(torch.nn.Module):
    def __init__(self, use_spectral_norm=False):
        super(MultiPeriodDiscriminator, self).__init__()
        periods = [2, 3, 5, 7, 11]

        discs = [DiscriminatorS(use_spectral_norm=use_spectral_norm)]
        discs = discs + [
            DiscriminatorP(i, use_spectral_norm=use_spectral_norm) for i in periods
        ]
        self.discriminators = nn.ModuleList(discs)

    def forward(self, y, y_hat):
        y_d_rs = []
        y_d_gs = []
        fmap_rs = []
        fmap_gs = []
        for i, d in enumerate(self.discriminators):
            y_d_r, fmap_r = d(y)
            y_d_g, fmap_g = d(y_hat)
            y_d_rs.append(y_d_r)
            y_d_gs.append(y_d_g)
            fmap_rs.append(fmap_r)
            fmap_gs.append(fmap_g)

        return y_d_rs, y_d_gs, fmap_rs, fmap_gs


class Projection(nn.Module):
    def __init__(self, hidden_channels, out_channels):
        super().__init__()
        self.hidden_channels = hidden_channels
        self.out_channels = out_channels
        self.proj = nn.Conv1d(hidden_channels, out_channels * 2, 1)

    def forward(self, x, x_mask):
        stats = self.proj(x) * x_mask
        m_p, logs_p = torch.split(stats, self.out_channels, dim=1)
        return m_p, logs_p


class ResidualConnectionModule(nn.Module):
    """
    Residual Connection Module.
    outputs = (module(inputs) x module_factor + inputs x input_factor)
    """

    def __init__(
        self, module: nn.Module, module_factor: float = 1.0, input_factor: float = 1.0
    ):
        super(ResidualConnectionModule, self).__init__()
        self.module = module
        self.module_factor = module_factor
        self.input_factor = input_factor

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return (self.module(inputs) * self.module_factor) + (inputs * self.input_factor)


class FramePriorBlock(nn.Module):
    def __init__(
        self, in_channels, filter_channels, kernel_size, p_dropout, gin_channels=0
    ):
        super().__init__()
        self.in_channels = in_channels
        self.filter_channels = filter_channels
        self.kernel_size = kernel_size
        self.p_dropout = p_dropout
        self.gin_channels = gin_channels

        self.sequential = nn.Sequential(
            ResidualConnectionModule(
                module=nn.Conv1d(
                    in_channels, filter_channels, kernel_size, padding=kernel_size // 2
                )
            ),
            nn.ReLU(),
            modules.LayerNorm(filter_channels),
            nn.Dropout(p_dropout),
        )

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return self.sequential(inputs)


class LengthRegulator(torch.nn.Module):
    """Length Regulator"""

    def __init__(self, pad_value=0.0):
        """Initilize length regulator module.
        Args:
            pad_value (float, optional): Value used for padding.
        """
        super().__init__()
        self.pad_value = pad_value
        self.winlen = 1024
        self.hoplen = 256
        self.sr = 16000

    def pad_list(self, xs, pad_value):
        """Perform padding for the list of tensors.

        Args:
            xs (List): List of Tensors [(T_1, `*`), (T_2, `*`), ..., (T_B, `*`)].
            pad_value (float): Value for padding.

        Returns:
            Tensor: Padded tensor (B, Tmax, `*`).

        Examples:
            >>> x = [torch.ones(4), torch.ones(2), torch.ones(1)]
            >>> x
            [tensor([1., 1., 1., 1.]), tensor([1., 1.]), tensor([1.])]
            >>> pad_list(x, 0)
            tensor([[1., 1., 1., 1.],
                    [1., 1., 0., 0.],
                    [1., 0., 0., 0.]])

        """
        n_batch = len(xs)
        max_len = max(x.size(0) for x in xs)
        pad = xs[0].new(n_batch, max_len, *xs[0].size()[1:]).fill_(pad_value)

        for i in range(n_batch):
            pad[i, : xs[i].size(0)] = xs[i]

        return pad

    def forward(self, xs, ds, x_lengths):
        if ds.sum() == 0:
            ds[ds.sum(dim=1).eq(0)] = 1

        # expand xs
        xs = torch.transpose(xs, 1, 2)
        phn_repeat = [torch.repeat_interleave(x, d, dim=0) for x, d in zip(xs, ds)]
        output = self.pad_list(phn_repeat, self.pad_value)  # (B, D_frame, dim)
        output = torch.transpose(output, 1, 2)

        x_lengths = torch.LongTensor([len(i) for i in phn_repeat]).to(output.device)

        return output, x_lengths


class PitchPredictor(nn.Module):
    def __init__(
        self,
        n_vocab,
        out_channels,
        hidden_channels,
        filter_channels,
        n_heads,
        n_layers,
        kernel_size,
        p_dropout,
    ):
        super().__init__()
        self.n_vocab = n_vocab  # 音素的个数，中文和英文不同
        self.out_channels = out_channels
        self.hidden_channels = hidden_channels
        self.filter_channels = filter_channels
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.kernel_size = kernel_size
        self.p_dropout = p_dropout

        self.pitch_net = attentions.Encoder(
            hidden_channels, filter_channels, n_heads, n_layers, kernel_size, p_dropout
        )
        self.proj = nn.Conv1d(hidden_channels, 1, 1)

    def forward(self, x, x_mask):
        pitch_embedding = self.pitch_net(x * x_mask, x_mask)
        pitch_embedding = pitch_embedding * x_mask
        pred_pitch = self.proj(pitch_embedding)
        return pred_pitch, pitch_embedding


class PhonemesPredictor(nn.Module):
    def __init__(
        self,
        n_vocab,
        out_channels,
        hidden_channels,
        filter_channels,
        n_heads,
        n_layers,
        kernel_size,
        p_dropout,
    ):
        super().__init__()
        self.n_vocab = n_vocab
        self.out_channels = out_channels
        self.hidden_channels = hidden_channels
        self.filter_channels = filter_channels
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.kernel_size = kernel_size
        self.p_dropout = p_dropout

        self.phonemes_predictor = attentions.Encoder(
            hidden_channels, filter_channels, n_heads, 2, kernel_size, p_dropout
        )
        self.linear1 = nn.Linear(hidden_channels, n_vocab)

    def forward(self, x, x_mask):
        phonemes_embedding = self.phonemes_predictor(x * x_mask, x_mask)
        # print("x_size:", x.size())
        x1 = self.linear1(phonemes_embedding.transpose(1, 2))
        x1 = x1.log_softmax(2)
        # print("phonemes_embedding size:", x1.size())
        return x1.transpose(0, 1)


class FramePriorNet(nn.Module):
    def __init__(
        self,
        n_vocab,
        out_channels,
        hidden_channels,
        filter_channels,
        n_heads,
        n_layers,
        kernel_size,
        p_dropout,
    ):
        super().__init__()

        self.n_vocab = n_vocab
        self.out_channels = out_channels
        self.hidden_channels = hidden_channels
        self.filter_channels = filter_channels
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.kernel_size = kernel_size
        self.p_dropout = p_dropout

        self.fft_block = attentions.Encoder(
            hidden_channels, filter_channels, n_heads, 4, kernel_size, p_dropout
        )

    def forward(self, x_frame, pitch_embedding, x_mask):
        x = x_frame + pitch_embedding
        x = self.fft_block(x * x_mask, x_mask)
        x = x.transpose(1, 2)
        return x


class SynthesizerTrn(nn.Module):
    """
    Synthesizer for Training
    """

    def __init__(
        self,
        n_vocab,
        spec_channels,
        segment_size,
        inter_channels,
        hidden_channels,
        filter_channels,
        n_heads,
        n_layers,
        kernel_size,
        p_dropout,
        resblock,
        resblock_kernel_sizes,
        resblock_dilation_sizes,
        upsample_rates,
        upsample_initial_channel,
        upsample_kernel_sizes,
        n_speakers=0,
        gin_channels=0,
        use_sdp=True,
        **kwargs
    ):

        super().__init__()
        self.spec_channels = spec_channels
        self.inter_channels = inter_channels
        self.hidden_channels = hidden_channels
        self.filter_channels = filter_channels
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.kernel_size = kernel_size
        self.p_dropout = p_dropout
        self.resblock = resblock
        self.resblock_kernel_sizes = resblock_kernel_sizes
        self.resblock_dilation_sizes = resblock_dilation_sizes
        self.upsample_rates = upsample_rates
        self.upsample_initial_channel = upsample_initial_channel
        self.upsample_kernel_sizes = upsample_kernel_sizes
        self.segment_size = segment_size
        self.n_speakers = n_speakers
        self.gin_channels = gin_channels

        self.use_sdp = use_sdp

        self.enc_p = TextEncoder(
            n_vocab,
            inter_channels,
            hidden_channels,
            filter_channels,
            n_heads,
            n_layers,
            kernel_size,
            p_dropout,
        )
        self.dec = Generator(
            inter_channels,
            resblock,
            resblock_kernel_sizes,
            resblock_dilation_sizes,
            upsample_rates,
            upsample_initial_channel,
            upsample_kernel_sizes,
            gin_channels=gin_channels,
        )
        self.enc_q = PosteriorEncoder(
            spec_channels,
            inter_channels,
            hidden_channels,
            5,
            1,
            16,
            gin_channels=gin_channels,
        )
        self.flow = ResidualCouplingBlock(
            inter_channels, hidden_channels, 5, 1, 4, gin_channels=gin_channels
        )

        self.dp = DurationPredictor(
            hidden_channels, 256, 3, 0.5, gin_channels=gin_channels
        )
        self.project = Projection(hidden_channels, inter_channels)
        self.lr = LengthRegulator()
        self.frame_prior_net = FramePriorNet(
            n_vocab,
            inter_channels,
            hidden_channels,
            filter_channels,
            n_heads,
            n_layers,
            kernel_size,
            p_dropout,
        )
        self.pitch_net = PitchPredictor(
            n_vocab,
            inter_channels,
            hidden_channels,
            filter_channels,
            n_heads,
            n_layers,
            kernel_size,
            p_dropout,
        )
        self.phonemes_predictor = PhonemesPredictor(
            n_vocab,
            inter_channels,
            hidden_channels,
            filter_channels,
            n_heads,
            n_layers,
            kernel_size,
            p_dropout,
        )
        self.ctc_loss = nn.CTCLoss(n_vocab - 1, reduction="mean")

        if n_speakers > 1:
            self.emb_g = nn.Embedding(n_speakers, gin_channels)

    def forward(
        self,
        phone,
        phone_lengths,
        phone_dur,
        score,
        score_dur,
        pitch,
        slurs,
        y,
        y_lengths,
        sid=None,
    ):
        x, x_mask = self.enc_p(phone, score, score_dur, slurs, phone_lengths)
        if self.n_speakers > 0:
            g = self.emb_g(sid).unsqueeze(-1)  # [b, h, 1]
        else:
            g = None

        # duration
        w = phone_dur.unsqueeze(1)
        gt_logw = w * x_mask
        pred_logw = self.dp(x, x_mask, score_dur, g=g)

        x_frame, x_lengths = self.lr(x, phone_dur, phone_lengths)

        x_frame = x_frame.to(x.device)
        x_mask = torch.unsqueeze(
            commons.sequence_mask(x_lengths, x_frame.size(2)), 1
        ).to(
            x.dtype
        )  # 更新x_mask矩阵
        x_mask = x_mask.to(x.device)
        max_len = x_frame.size(2)
        d_model = x_frame.size(1)
        batch_size = x_frame.size(0)
        pe = torch.zeros(batch_size, max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2) * -(math.log(10000.0) / d_model)
        )
        pe[:, :, 0::2] = torch.sin(position * div_term)
        pe[:, :, 1::2] = torch.cos(position * div_term)
        pe = pe.transpose(1, 2).to(x_frame.device)
        x_frame = x_frame + pe

        pred_pitch, pitch_embedding = self.pitch_net(x_frame, x_mask)
        lf0 = torch.unsqueeze(pred_pitch, -1)
        gt_lf0 = pitch.to(torch.float32)
        pred_lf0 = lf0.squeeze()

        x_frame = self.frame_prior_net(x_frame, pitch_embedding, x_mask)
        x_frame = x_frame.transpose(1, 2)

        m_p, logs_p = self.project(x_frame, x_mask)

        z, m_q, logs_q, y_mask = self.enc_q(y, y_lengths, g=g)

        log_probs = self.phonemes_predictor(z, y_mask)
        # print("log_probs", log_probs.shape)
        # print("phone", phone.shape)
        # print("y_lengths", y_lengths)
        # print("phone_lengths", phone_lengths)
        ctc_loss = self.ctc_loss(log_probs, phone, y_lengths, phone_lengths)

        z_p = self.flow(z, y_mask, g=g)

        z_slice, ids_slice = commons.rand_slice_segments(
            z, y_lengths, self.segment_size
        )
        o = self.dec(z_slice, g=g)
        return (
            o,
            ids_slice,
            x_mask,
            y_mask,
            (z, z_p, m_p, logs_p, m_q, logs_q),
            gt_logw,
            pred_logw,
            gt_lf0,
            pred_lf0,
            ctc_loss,
        )

    def infer(
        self,
        phone,
        phone_lengths,
        phone_dur,
        score,
        score_dur,
        pitch,
        slurs,
        y,
        y_lengths,
        sid=None,
    ):
        x, x_mask = self.enc_p(phone, score, score_dur, slurs, phone_lengths)
        if self.n_speakers > 0:
            g = self.emb_g(sid).unsqueeze(-1)  # [b, h, 1]
        else:
            g = None

        # duration
        w = phone_dur.unsqueeze(1)
        gt_logw = w * x_mask
        pred_logw = self.dp(x, x_mask, score_dur, g=g)
        # pred_logw_ = (pred_logw * x_mask).type(torch.LongTensor).to(x.device).squeeze(1)
        x_frame, x_lengths = self.lr(x, phone_dur, phone_lengths)

        x_mask = torch.unsqueeze(
            commons.sequence_mask(x_lengths, x_frame.size(2)), 1
        ).to(x.dtype)
        x_mask = x_mask.to(x.device)
        max_len = x_frame.size(2)
        d_model = x_frame.size(1)
        batch_size = x_frame.size(0)
        pe = torch.zeros(batch_size, max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2) * -(math.log(10000.0) / d_model)
        )
        pe[:, :, 0::2] = torch.sin(position * div_term)
        pe[:, :, 1::2] = torch.cos(position * div_term)
        pe = pe.transpose(1, 2).to(x_frame.device)
        x_frame = x_frame + pe

        pred_pitch, pitch_embedding = self.pitch_net(x_frame, x_mask)
        lf0 = torch.unsqueeze(pred_pitch, -1)
        gt_lf0 = pitch.to(torch.float32)
        pred_lf0 = lf0.squeeze()

        x_frame = self.frame_prior_net(x_frame, pitch_embedding, x_mask)
        x_frame = x_frame.transpose(1, 2)
        m_p, logs_p = self.project(x_frame, x_mask)

        z, m_q, logs_q, y_mask = self.enc_q(y, y_lengths, g=g)

        log_probs = self.phonemes_predictor(z, y_mask)

        ctc_loss = self.ctc_loss(log_probs, phone, y_lengths, phone_lengths)
        z_p_ori = self.flow(z, y_mask, g=g)
        z_p = m_p + torch.randn_like(m_p) * torch.exp(logs_p) * 0.3
        z = self.flow(z_p, x_mask, g=g, reverse=True)

        z_slice, ids_slice = commons.rand_slice_segments(
            z, y_lengths, self.segment_size
        )
        o = self.dec(z_slice, g=g)
        return (
            o,
            ids_slice,
            x_mask,
            y_mask,
            (z, z_p_ori, m_p, logs_p, m_q, logs_q),
            gt_logw,
            pred_logw,
            gt_lf0,
            pred_lf0,
            ctc_loss,
        )

    def voice_conversion(self, y, y_lengths):
        z, m_q, logs_q, y_mask = self.enc_q(y, y_lengths, g=None)
        z_p = self.flow(z, y_mask, g=None)
        z_hat = self.flow(z_p, y_mask, g=None, reverse=True)
        o_hat = self.dec(z_hat * y_mask, g=None)
        return o_hat, y_mask, (z, z_p, z_hat)


class Synthesizer(nn.Module):
    """
    Synthesizer for Training
    """

    def __init__(
        self,
        n_vocab,
        spec_channels,
        segment_size,
        inter_channels,
        hidden_channels,
        filter_channels,
        n_heads,
        n_layers,
        kernel_size,
        p_dropout,
        resblock,
        resblock_kernel_sizes,
        resblock_dilation_sizes,
        upsample_rates,
        upsample_initial_channel,
        upsample_kernel_sizes,
        n_speakers=0,
        gin_channels=0,
        use_sdp=True,
        **kwargs
    ):

        super().__init__()
        self.spec_channels = spec_channels
        self.inter_channels = inter_channels
        self.hidden_channels = hidden_channels
        self.filter_channels = filter_channels
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.kernel_size = kernel_size
        self.p_dropout = p_dropout
        self.resblock = resblock
        self.resblock_kernel_sizes = resblock_kernel_sizes
        self.resblock_dilation_sizes = resblock_dilation_sizes
        self.upsample_rates = upsample_rates
        self.upsample_initial_channel = upsample_initial_channel
        self.upsample_kernel_sizes = upsample_kernel_sizes
        self.segment_size = segment_size
        self.n_speakers = n_speakers
        self.gin_channels = gin_channels

        self.enc_p = TextEncoder(
            n_vocab,
            inter_channels,
            hidden_channels,
            filter_channels,
            n_heads,
            n_layers,
            kernel_size,
            p_dropout,
        )
        self.dec = Generator(
            inter_channels,
            resblock,
            resblock_kernel_sizes,
            resblock_dilation_sizes,
            upsample_rates,
            upsample_initial_channel,
            upsample_kernel_sizes,
            gin_channels=gin_channels,
        )
        self.enc_q = PosteriorEncoder(
            spec_channels,
            inter_channels,
            hidden_channels,
            5,
            1,
            16,
            gin_channels=gin_channels,
        )
        self.flow = ResidualCouplingBlock(
            inter_channels, hidden_channels, 5, 1, 4, gin_channels=gin_channels
        )

        self.dp = DurationPredictor(
            hidden_channels, 256, 3, 0.5, gin_channels=gin_channels
        )
        self.project = Projection(hidden_channels, inter_channels)
        self.lr = LengthRegulator()
        self.frame_prior_net = FramePriorNet(
            n_vocab,
            inter_channels,
            hidden_channels,
            filter_channels,
            n_heads,
            n_layers,
            kernel_size,
            p_dropout,
        )
        self.pitch_net = PitchPredictor(
            n_vocab,
            inter_channels,
            hidden_channels,
            filter_channels,
            n_heads,
            n_layers,
            kernel_size,
            p_dropout,
        )
        self.phonemes_predictor = PhonemesPredictor(
            n_vocab,
            inter_channels,
            hidden_channels,
            filter_channels,
            n_heads,
            n_layers,
            kernel_size,
            p_dropout,
        )

    def infer(
        self, phone, phone_lengths, score, score_dur, slurs, sid=None, max_len=None
    ):
        x, x_mask = self.enc_p(phone, score, score_dur, slurs, phone_lengths)
        if self.n_speakers > 0:
            g = self.emb_g(sid).unsqueeze(-1)  # [b, h, 1]
        else:
            g = None

        logw = self.dp(x, x_mask, score_dur, g=g)
        # logw = torch.mul(logw.squeeze(1), score_dur).unsqueeze(1)
        w = (logw * x_mask).type(torch.LongTensor).to(x.device).squeeze(1)
        x_frame, x_lengths = self.lr(x, w, phone_lengths)
        x_frame = x_frame.to(x.device)
        x_mask = torch.unsqueeze(
            commons.sequence_mask(x_lengths, x_frame.size(2)), 1
        ).to(x.dtype)
        x_mask = x_mask.to(x.device)
        max_len = x_frame.size(2)
        d_model = x_frame.size(1)
        batch_size = x_frame.size(0)
        pe = torch.zeros(batch_size, max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2) * -(math.log(10000.0) / d_model)
        )
        pe[:, :, 0::2] = torch.sin(position * div_term)
        pe[:, :, 1::2] = torch.cos(position * div_term)
        pe = pe.transpose(1, 2).to(x_frame.device)
        x_frame = x_frame + pe
        pred_pitch, pitch_embedding = self.pitch_net(x_frame, x_mask)

        x_frame = self.frame_prior_net(x_frame, pitch_embedding, x_mask)
        x_frame = x_frame.transpose(1, 2)
        m_p, logs_p = self.project(x_frame, x_mask)

        z_p = m_p + torch.randn_like(m_p) * torch.exp(logs_p) * 0.3
        z = self.flow(z_p, x_mask, g=g, reverse=True)
        o = self.dec((z * x_mask)[:, :, :max_len], g=g)
        return o, x_mask, (z, z_p, m_p, logs_p)

    def remove_weight_norm(self):
        self.flow.remove_weight_norm()
        self.dec.remove_weight_norm()
