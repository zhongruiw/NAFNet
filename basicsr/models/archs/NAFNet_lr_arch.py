# ------------------------------------------------------------------------
# Copyright (c) 2022 megvii-model. All Rights Reserved.
# ------------------------------------------------------------------------

'''
Simple Baselines for Image Restoration

@article{chen2022simple,
  title={Simple Baselines for Image Restoration},
  author={Chen, Liangyu and Chu, Xiaojie and Zhang, Xiangyu and Sun, Jian},
  journal={arXiv preprint arXiv:2204.04676},
  year={2022}
}
'''

import torch
import torch.nn as nn
import torch.nn.functional as F
from basicsr.models.archs.arch_util import LayerNorm2d
from basicsr.models.archs.local_arch import Local_Base
from torchsummary import summary
import torch.nn.utils.parametrize as parametrize
from torch.nn.functional import softplus


class _LipNorm(nn.Module):
    def __init__(self, weight: torch.Tensor):
        super().__init__()
        self.ci = nn.Parameter(torch.randn(1), requires_grad=True)

    def _reshape_weight_to_matrix(self, weight: torch.Tensor) -> torch.Tensor:
        # Precondition
        assert weight.ndim > 1

#         if self.dim != 0:
#             # permute dim to front
#             weight = weight.permute(self.dim, *(d for d in range(weight.dim()) if d != self.dim))

        return weight.flatten(1)
    
    def forward(self, weight: torch.Tensor):
#         assert weight.ndim == 2
        weight_mat = self._reshape_weight_to_matrix(weight)
        
        softplus_ci = softplus(self.ci)
        absrowsum = torch.sum(torch.abs(weight_mat), dim=1)
        scale = torch.min(torch.tensor(1.0), softplus_ci / absrowsum)

        return weight * scale[:,None,None,None]
        
    def right_inverse(self, value: torch.Tensor) -> torch.Tensor:
        # we may want to assert here that the passed value already
        # satisfies constraints
        return value

def LipNorm(module: nn.Module,name: str = 'weight') -> nn.Module:
    weight = getattr(module, name, None)

    if not isinstance(weight, torch.Tensor):
        raise ValueError(
            "Module '{}' has no parameter or buffer with name '{}'".format(module, name)
        )

    parametrize.register_parametrization(module, name, _LipNorm(weight))
    return module

class SimpleGate(nn.Module):
    def forward(self, x):
        x1, x2 = x.chunk(2, dim=1)
        return x1 * x2

class NAFBlock(nn.Module):
    def __init__(self, c, DW_Expand=2, FFN_Expand=2, drop_out_rate=0.):
        super().__init__()
        dw_channel = c * DW_Expand
        self.conv1 = LipNorm(nn.Conv2d(in_channels=c, out_channels=dw_channel, kernel_size=1, padding=0, stride=1, groups=1, bias=True))
        self.conv2 = LipNorm(nn.Conv2d(in_channels=dw_channel, out_channels=dw_channel, kernel_size=3, padding=0, stride=1, groups=dw_channel,
                               bias=True))
        self.conv3 = LipNorm(nn.Conv2d(in_channels=dw_channel // 2, out_channels=c, kernel_size=1, padding=0, stride=1, groups=1, bias=True))
        
        # Simplified Channel Attention
        self.sca = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            LipNorm(nn.Conv2d(in_channels=dw_channel // 2, out_channels=dw_channel // 2, kernel_size=1, padding=0, stride=1,
                      groups=1, bias=True)),
        )

        # SimpleGate
        self.sg = SimpleGate()

        ffn_channel = FFN_Expand * c
        self.conv4 = LipNorm(nn.Conv2d(in_channels=c, out_channels=ffn_channel, kernel_size=1, padding=0, stride=1, groups=1, bias=True))
        self.conv5 = LipNorm(nn.Conv2d(in_channels=ffn_channel // 2, out_channels=c, kernel_size=1, padding=0, stride=1, groups=1, bias=True))

        self.norm1 = LayerNorm2d(c)
        self.norm2 = LayerNorm2d(c)

        self.dropout1 = nn.Dropout(drop_out_rate) if drop_out_rate > 0. else nn.Identity()
        self.dropout2 = nn.Dropout(drop_out_rate) if drop_out_rate > 0. else nn.Identity()

        self.beta = nn.Parameter(torch.zeros((1, c, 1, 1)), requires_grad=True)
        self.gamma = nn.Parameter(torch.zeros((1, c, 1, 1)), requires_grad=True)

    def forward(self, inp):
        x = inp

        x = self.norm1(x)

        x = self.conv1(x)
        x = self.CircularPadding(x)
        x = self.conv2(x)
        x = self.sg(x)
        x = x * self.sca(x)
        x = self.conv3(x)

        x = self.dropout1(x)

        y = inp + x * self.beta

        x = self.conv4(self.norm2(y))
        # x = self.conv4(y) # if no layer normalization 
        x = self.sg(x)
        x = self.conv5(x)

        x = self.dropout2(x)

        return y + x * self.gamma

    def CircularPadding(self, inp):
        _, _, H, W = inp.shape
        kht, kwd = [3, 3]
        sht, swd = [1, 1]
        assert kwd%2 != 0 and kht%2 !=0 and (W-kwd)%swd==0 and (H-kht)%sht ==0, 'kernel_size should be odd, (dim-kernel_size) should be divisible by stride'

        pwd = int((W - 1 - (W - kwd) / swd) // 2)
        pht = int((H - 1 - (H - kht) / sht) // 2)
        
        # kht1, kwd1 = self.kernel_sizes[1]
        # kht2, kwd2 = self.kernel_sizes[2]
        # pwd = int((W - 1 - (W - kwd) / swd) // 2 + (W - 1 - (W - kwd1) / swd) // 2 + (W - 1 - (W - kwd2) / swd) // 2)
        # pht = int((H - 1 - (H - kht) / sht) // 2 + (H - 1 - (H - kht1) / sht) // 2 + (H - 1 - (H - kht2) / sht) // 2)
        
        x = F.pad(inp, (pwd, pwd, pht, pht), 'circular')

        return x


class NAFNet_lr(nn.Module):

    def __init__(self, img_channel=3, width=16, middle_blk_num=1, enc_blk_nums=[], dec_blk_nums=[]):
        super().__init__()

        self.intro = LipNorm(nn.Conv2d(in_channels=img_channel, out_channels=width, kernel_size=3, padding=0, stride=1, groups=1,
                              bias=True))
        self.ending = LipNorm(nn.Conv2d(in_channels=width, out_channels=img_channel, kernel_size=3, padding=0, stride=1, groups=1,
                              bias=True))

        self.encoders = nn.ModuleList()
        self.decoders = nn.ModuleList()
        self.middle_blks = nn.ModuleList()
        self.ups = nn.ModuleList()
        self.downs = nn.ModuleList()

        chan = width
        for num in enc_blk_nums:
            self.encoders.append(
                nn.Sequential(
                    *[NAFBlock(chan) for _ in range(num)]
                )
            )
            self.downs.append(
                LipNorm(nn.Conv2d(chan, 2*chan, 2, 2))
            )
            chan = chan * 2

        self.middle_blks = \
            nn.Sequential(
                *[NAFBlock(chan) for _ in range(middle_blk_num)]
            )

        for num in dec_blk_nums:
            self.ups.append(
                nn.Sequential(
                    LipNorm(nn.Conv2d(chan, chan * 2, 1, bias=False)),
                    nn.PixelShuffle(2)
                )
            )
            chan = chan // 2
            self.decoders.append(
                nn.Sequential(
                    *[NAFBlock(chan) for _ in range(num)]
                )
            )

        self.padder_size = 2 ** len(self.encoders)

    def forward(self, inp):
        B, C, H, W = inp.shape
        inp = self.check_image_size(inp)

        x = self.CircularPadding(inp)
        x = self.intro(x)

        encs = []

        for encoder, down in zip(self.encoders, self.downs):
            x = encoder(x)
            encs.append(x)
            x = down(x)

        x = self.middle_blks(x)

        for decoder, up, enc_skip in zip(self.decoders, self.ups, encs[::-1]):
            x = up(x)
            x = x + enc_skip
            x = decoder(x)

        x = self.CircularPadding(x)
        x = self.ending(x)
        x = x + inp

        return x[:, :, :H, :W]

    def check_image_size(self, x):
        _, _, h, w = x.size()
        mod_pad_h = (self.padder_size - h % self.padder_size) % self.padder_size
        mod_pad_w = (self.padder_size - w % self.padder_size) % self.padder_size
        x = F.pad(x, (0, mod_pad_w, 0, mod_pad_h), 'circular')
        return x

    def CircularPadding(self, inp):
        _, _, H, W = inp.shape
        kht, kwd = [3, 3]
        sht, swd = [1, 1]
        assert kwd%2 != 0 and kht%2 !=0 and (W-kwd)%swd==0 and (H-kht)%sht ==0, 'kernel_size should be odd, (dim-kernel_size) should be divisible by stride'

        pwd = int((W - 1 - (W - kwd) / swd) // 2)
        pht = int((H - 1 - (H - kht) / sht) // 2)
        
        # kht1, kwd1 = self.kernel_sizes[1]
        # kht2, kwd2 = self.kernel_sizes[2]
        # pwd = int((W - 1 - (W - kwd) / swd) // 2 + (W - 1 - (W - kwd1) / swd) // 2 + (W - 1 - (W - kwd2) / swd) // 2)
        # pht = int((H - 1 - (H - kht) / sht) // 2 + (H - 1 - (H - kht1) / sht) // 2 + (H - 1 - (H - kht2) / sht) // 2)
        
        x = F.pad(inp, (pwd, pwd, pht, pht), 'circular')

        return x


class NAFNetLocal(Local_Base, NAFNet_lr):
    def __init__(self, *args, train_size=(1, 3, 256, 256), fast_imp=False, **kwargs):
        Local_Base.__init__(self)
        NAFNet_lr.__init__(self, *args, **kwargs)

        N, C, H, W = train_size
        base_size = (int(H * 1.5), int(W * 1.5))

        self.eval()
        with torch.no_grad():
            self.convert(base_size=base_size, train_size=train_size, fast_imp=fast_imp)


if __name__ == '__main__':
    import resource
    def using(point=""):
        # print(f'using .. {point}')
        usage = resource.getrusage(resource.RUSAGE_SELF)
        global Total, LastMem

        # if usage[2]/1024.0 - LastMem > 0.01:
        # print(point, usage[2]/1024.0)
        print(point, usage[2] / 1024.0)

        LastMem = usage[2] / 1024.0
        return usage[2] / 1024.0

    img_channel = 1
    img_ht = 960
    img_wd = 240
    width = 32
    
    enc_blks = [1, 1, 2, 8]
    middle_blk_num = 4
    dec_blks = [1, 1, 1, 1]
    
    print('enc blks', enc_blks, 'middle blk num', middle_blk_num, 'dec blks', dec_blks, 'width' , width)
    
    using('start . ')
    net = NAFNet_lr(img_channel=img_channel, width=width, middle_blk_num=middle_blk_num, 
                      enc_blk_nums=enc_blks, dec_blk_nums=dec_blks)

    using('network .. ')

    # for n, p in net.named_parameters()
    #     print(n, p.shape)

    inp = torch.randn((4, img_channel, img_ht, img_wd))

    out = net(inp)
    final_mem = using('end .. ')
    # out.sum().backward()

    # out.sum().backward()

    # using('backward .. ')

    # exit(0)

    # keras like model summary
    summary(net, input_size=(img_channel, img_ht, img_wd), device='cpu')

    # inp_shape = (3, 512, 512)

    # from ptflops import get_model_complexity_info

    # macs, params = get_model_complexity_info(net, inp_shape, verbose=False, print_per_layer_stat=False)

    # params = float(params[:-3])
    # macs = float(macs[:-4])

    # print(macs, params)

    # print('total .. ', params * 8 + final_mem)



