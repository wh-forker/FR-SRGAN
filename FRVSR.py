import unittest
import torch
import torch.nn as nn
import torch.nn.functional as func

class ResBlock(nn.Module):
    def __init__(self, conv_dim):
        super(ResBlock, self).__init__()
        self.conv1 = nn.Conv2d(in_channels=conv_dim, out_channels=conv_dim, kernel_size=3, stride=1, padding=1)
        self.conv2 = nn.Conv2d(in_channels=conv_dim, out_channels=conv_dim, kernel_size=3, stride=1, padding=1)

    def forward(self, x):
        out = self.conv1(x)
        out = func.relu(out)
        out = self.conv2(out)
        out = x + out
        return out

class ConvLeaky(nn.Module):
    def __init__(self, in_dim, out_dim):
        super(ConvLeaky, self).__init__()
        self.conv1 = nn.Conv2d(in_channels=in_dim, out_channels=out_dim, kernel_size=3, stride=1, padding=1)
        self.conv2 = nn.Conv2d(in_channels=out_dim, out_channels=out_dim, kernel_size=3, stride=1, padding=1)

    def forward(self, x):
        out = self.conv1(x)
        out = func.leaky_relu(out, 0.2)
        out = self.conv2(out)
        out = func.leaky_relu(out, 0.2)
        return out

class FNetBlock(nn.Module):
    def __init__(self, in_dim, out_dim, typ):
        super(FNetBlock, self).__init__()
        self.convleaky = ConvLeaky(in_dim, out_dim)
        if typ == "maxpool":
            self.final = lambda x: func.max_pool2d(x, kernel_size=2)
        elif typ == "bilinear":
            self.final = lambda x: func.interpolate(x, scale_factor=2, mode="bilinear")
        else:
            raise Exception('typ does not match any of maxpool or bilinear')

    def forward(self, x):
        out = self.convleaky(x)
        out = self.final(out)
        return out

class SRNet(nn.Module):
    def __init__(self, in_dim=51):
        super(SRNet, self).__init__()
        self.inputConv = nn.Conv2d(in_channels=in_dim, out_channels=64, kernel_size=3, stride=1, padding=1)
        self.ResBlocks = nn.Sequential(*[ResBlock(64) for i in range(10)])
        self.deconv1 = nn.ConvTranspose2d(in_channels=64, out_channels=64, kernel_size=3, stride=2, padding=1, output_padding=1)
        self.deconv2 = nn.ConvTranspose2d(in_channels=64, out_channels=64, kernel_size=3, stride=2, padding=1, output_padding=1)
        self.outputConv = nn.Conv2d(in_channels=64, out_channels=3, kernel_size=3, stride=1, padding=1)

    def forward(self, x):
        out = self.inputConv(x)
        out = self.ResBlocks(out)
        out = self.deconv1(out)
        out = func.relu(out)
        out = self.deconv2(out)
        out = func.relu(out)
        out = self.outputConv(out)
        return out

class FNet(nn.Module):
    def __init__(self, in_dim=6):
        super(FNet, self).__init__()
        self.convPool1 = FNetBlock(in_dim, 32, typ="maxpool")
        self.convPool2 = FNetBlock(32, 64, typ="maxpool")
        self.convPool3 = FNetBlock(64, 128, typ="maxpool")
        self.convBinl1 = FNetBlock(128, 256, typ="bilinear")
        self.convBinl2 = FNetBlock(256, 128, typ="bilinear")
        self.convBinl3 = FNetBlock(128, 64, typ="bilinear")
        self.seq = nn.Sequential(self.convPool1, self.convPool2, self.convPool3, self.convBinl1, self.convBinl2, self.convBinl3)
        self.conv1 = nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, stride=1, padding=1)
        self.conv2 = nn.Conv2d(in_channels=32, out_channels=2, kernel_size=3, stride=1, padding=1)

    def forward(self, x):
        out = self.seq(x)
        out = self.conv1(out)
        out = func.leaky_relu(out, 0.2)
        out = self.conv2(out)
        out = func.tanh(out)
        return out

# please ensure that input is (batch_size, depth, hegiht, width)
# courtesy to Hung Nguyen at https://gist.github.com/jalola/f41278bb27447bed9cd3fb48ec142aec.
class SpaceToDepth(nn.Module):
    def __init__(self, block_size):
        super(SpaceToDepth, self).__init__()
        self.block_size = block_size
        self.block_size_sq = block_size*block_size

    def forward(self, input):
        output = input.permute(0, 2, 3, 1)
        (batch_size, s_height, s_width, s_depth) = output.size()
        d_depth = s_depth * self.block_size_sq
        d_width = int(s_width / self.block_size)
        d_height = int(s_height / self.block_size)
        t_1 = output.split(self.block_size, 2)
        stack = [t_t.reshape(batch_size, d_height, d_depth) for t_t in t_1]
        output = torch.stack(stack, 1)
        output = output.permute(0, 2, 1, 3)
        output = output.permute(0, 3, 1, 2)
        return output

class FRVSR(nn.Module):
    def __init__(self, batch_size, lrHeight, lrWidth):
        super(FRVSR, self).__init__()
        FRVSR.SRFactor = 4
        self.width=lrWidth
        self.height=lrHeight
        self.batch_size = batch_size
        self.fnet = FNet()
        self.todepth = SpaceToDepth(4)
        self.srnet = SRNet()

    # make sure to call this before every batch train.
    def init_hidden(self):
        self.lastLrImg = torch.zeros([self.batch_size, 3, self.height, self.width])
        self.lastEstImg = torch.zeros([self.batch_size, 3, self.height*FRVSR.SRFactor, self.width*FRVSR.SRFactor])

    # x is a 4-d tensor of shape N×C×H×W
    def forward(self, x):
        preflow = torch.cat((x, self.lastLrImg), dim=1)
        flow = self.fnet(preflow)
        flowNCHW = func.interpolate(flow, scale_factor=4, mode="bilinear")
        flowNHWC = flowNCHW.permute(0, 2, 3, 1) # shift c to last, as grid_sample function need it.
        afterWarp = func.grid_sample(self.lastEstImg, flowNHWC)
        depthImg = self.todepth(afterWarp)
        srInput = torch.cat((x, depthImg), dim=1)
        estImg = self.srnet(srInput)
        self.lastLrImg = x
        self.lastEstImg = estImg
        return estImg

class TestFRVSR(unittest.TestCase):
    def testResBlock(self):
        block = ResBlock(3)
        input = torch.rand(2,3,64,112)
        output = block(input)
        self.assertEquals(input.shape, output.shape)

    def testConvLeaky(self):
        block = ConvLeaky(3, 32)
        input = torch.rand(2,3,64,112)
        output = block(input)
        self.assertEquals(output.shape, torch.empty(2,32,64,112).shape)

    def testFNetBlockMaxPool(self):
        block = FNetBlock(3, 32, "maxpool")
        input = torch.rand(2,3,64,112)
        output = block(input)
        self.assertEquals(output.shape, torch.empty(2, 32, 32, 56).shape)

    def testFNetBlockInterPolate(self):
        block = FNetBlock(3, 32, "bilinear")
        input = torch.rand(2,3, 32, 56)
        output = block(input)
        self.assertEquals(output.shape, torch.empty(2, 32, 64,112).shape)

    def testSRNet(self):
        block = SRNet()
        input = torch.rand(2, 51, 32, 56)
        output = block(input)
        self.assertEquals(output.shape, torch.empty(2, 3, 128, 224).shape)
        block = SRNet()
        input = torch.rand(2, 51, 64, 64)
        output = block(input)
        self.assertEquals(output.shape, torch.empty(2, 3, 256, 256).shape)

    def testFNet(self):
        block = FNet()
        input = torch.rand(2, 6, 32, 56)
        output = block(input)
        self.assertEquals(output.shape, torch.empty(2, 2, 32, 56).shape)

    def testFRVSR(self):
        H = 16
        W = 16
        block = FRVSR(4, H, W)
        input = torch.rand(7, 4, 3, H, W)
        block.init_hidden()
        for batch_frames in input:
            output = block(batch_frames)
            self.assertEquals(output.shape, torch.empty(4, 3, H*4, W*4).shape)

if __name__ == '__main__':
    unittest.main()