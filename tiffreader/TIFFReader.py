from tifffile import TiffFile
from glob import glob
import os
from oct2py import Oct2Py
import re
import numpy as np
from . import VersionNumberException

si_verions = {
    4: re.compile("""^scanimage\.SI4\.(?P<attr>\w*)\s*=\s*(?P<value>.*\S)\s*$"""),
    5: re.compile("""^scanimage\.SI\.(?P<attr>[\.\w]*)\s*=\s*(?P<value>.*\S)\s*$"""),
    5.2: re.compile("""^SI\.(?P<attr>[\.\w]*)\s*=\s*(?P<value>.*\S)\s*$""")
}


def get_scanimage_version_and_header(hdr):
    o2p = Oct2Py()
    for version, vregexp in si_verions.items():
        tmp = [vregexp.match(s) for s in hdr if vregexp.match(s) is not None]
        if len(tmp) > 0:
            print('Found scan image version', version)
            hdr = {g['attr']: g['value'] for g in map(lambda x: x.groupdict(), tmp)}
            break
    else:
        raise VersionNumberException("Cannot find header information. Possibly wrong scanimage version")

    hdr_ret = {}
    for k, v in hdr.items():
        if not v[0] == "<" and not v[-1] == '>':
            hdr_ret[k.replace('.', '_')] = o2p.eval(v, verbose=False)

    return version, hdr_ret


class TIFFReader:
    def __init__(self, wildcard):
        if isinstance(wildcard, list):
            self._files = sorted(map(os.path.abspath, wildcard), key=lambda x: x.split('/')[-1])
        else:
            self._files = sorted(map(os.path.abspath, glob(wildcard)), key=lambda x: x.split('/')[-1])
        self._stacks = [TiffFile(file, fastij=True) for file in self._files]
        self._n = [len(s.pages) for s in self._stacks]
        self._ntiffs = sum(self._n)
        self.load_header()
        self._i2j = np.vstack([np.c_[i * np.ones(nn), np.arange(nn)] for i, nn in enumerate(self._n)]).astype(int)
        if not self.is_structural:
            self._idx = np.reshape(np.arange(self._ntiffs, dtype=int),
                                   (self.nframes, self.nslices, self.nchannels)).transpose()
        else:
            self._idx = np.reshape(np.arange(self._ntiffs, dtype=int),
                                   (self.nslices, self.nframes, self.nchannels)).transpose([2, 0, 1])
        self._img_dim = None

    def load_header(self):
        first_frame = self._stacks[0].pages[0]
        tag = first_frame.tags['software'] if 'software' in first_frame.tags else first_frame.tags['image_description']
        hdr = [s.strip() for s in tag.value.decode('utf-8').split('\n')]
        self.scanimage_version, self.header = get_scanimage_version_and_header(hdr)

    @property
    def channels(self):
        ret = self.header['channelsSave'] if self.scanimage_version == 4 else self.header['hChannels_channelSave']
        return ret.squeeze()

    @property
    def nslices(self):
        if self.scanimage_version == 4:
            return int(self.header['stackNumSlices'])
        else:
            return int(self.header['hStackManager_numSlices'])

    @property
    def fill_fraction(self):
        return self.header['scanFillFraction'] if self.scanimage_version == 4 else self.header[
            'hScan2D_fillFractionTemporal']

    @property
    def fps(self):
        if self.scanimage_version == 4:
            if self.header['fastZactive']:
                fps = 1 / self.header['fastZPeriod']
            else:
                assert self.nslices == 1
                fps = self.header['scanFrameRate']
        else:
            if self.nslices >= 1:
                fps = self.header['hRoiManager_scanVolumeRate']
            else:
                fps = self.header['hRoiManager_scanFrameRate']
        return fps

    @property
    def slice_pitch(self):
        if self.scanimage_version == 4:
            if self.header['fastZActive']:
                p = self.header['stackZStepSize']
            else:
                p = 0
        else:
            p = self.header['hStackManager_stackZStepSize']

        return p

    @property
    def is_structural(self):
        return self.header['hFastZ_enable'] == 0

    @property
    def requested_frames(self):
        if self.scanimage_version == 4:
            if self.header['fastZActive']:
                n = self.header['fastZNumVolumes']
            else:
                n = self.header['acqNumFrames']
        else:
            n = self.header['hFastZ_numVolumes']
        return int(n)

    @property
    def nframes(self):
        return int(self._ntiffs / self.nchannels / self.nslices)

    @property
    def bidirectional(self):
        return bool(self.header['scanMode'] == 'uni' if self.scanimage_version == 4
                    else self.header['hScan2D_bidirectional'])

    @property
    def dwell_time(self):
        return self.header['scanPixelTimeMean'] * 1e6 if self.scanimage_version == 4 \
            else self.header['hScan2D_scanPixelTimeMean'] * 1e6

    @property
    def nchannels(self):
        return len(self.channels)

    @property
    def zoom(self):
        return self.header['scanZoomFactor'] if self.scanimage_version == 4 \
            else self.header['hRoiManager_scanZoomFactor']

    @property
    def shape(self):
        if self._img_dim is None:
            self._img_dim = self._stacks[0].asarray([0]).shape
        return self._img_dim + (self.nchannels, self.nslices, self.nframes)

    def __getitem__(self, item):
        for i in item:
            if i is Ellipsis:
                raise IndexError('Not supporting ... yet')

        # split into indices into the image and into channel, slice, frames
        img_slice, vol_slice = item[:2], item[2:]
        img_shape = self.shape[:2]

        # create a reshaping array to ensure that the indices have the correct dimensions later
        # this is a fancy version of np.atleast_2d
        shape = tuple((slice(None) if isinstance(i, slice) else None for i in vol_slice))

        # get frame indices and make the dimensions right
        idx = self._idx[vol_slice][shape]

        # create the return value
        ret_val = np.empty(img_shape + idx.shape, dtype=np.int16)

        # from the frame indices extract the stacknumber (column 0) and the frame number within a column (column 1)
        stack_idx = self._i2j[idx.ravel()]

        # that is used with logical indexing to put the images back in place
        file_id = stack_idx[:, 0].reshape(idx.shape)

        for f in np.unique(stack_idx[:, 0]):  # in case we extract data from more than one stack file
            file_frames = stack_idx[stack_idx[:, 0] == f, 1]  # get frames for current file

            # extract images and reshape back in order
            tmp = self._stacks[f].asarray(file_frames)
            if len(tmp.shape) == 2:
                ret_val[..., file_id == f] = tmp[..., None]
            else:
                ret_val[..., file_id == f] = tmp.transpose([1, 2, 0])

        # extract image dimensions if specified
        return ret_val[img_slice + 3 * (slice(None),)]
