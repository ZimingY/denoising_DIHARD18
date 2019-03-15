#!/usr/bin/env python
"""Perform denoising of audio stored in WAV files.

References
----------
Sun, Lei, et al. "Speaker diarization with enhancing speech for the First DIHARD
Challenge." Proceedings of INTERSPEECH 2019. 2793-2797.
"""
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
import argparse
import math
from multiprocessing import Process
import os
import shutil
import sys
import tempfile

import numpy as np
import scipy.io.wavfile as wav_io
import scipy.io as sio

from decode_model import decode_model
import utils

HERE = os.path.abspath(os.path.dirname(__file__))
GLOBAL_MEAN_VAR_MATF = os.path.join(HERE, 'model', 'global_mvn_stats.mat')


SR = 16000 # Sample rate of files in Hz.
WL = 512 # Analysis window length in samples for feature extraction.
WL2 = WL // 2
NFREQS = 257 # Number of positive frequencies in FFT output.


def main_denoising(wav_files, out_dir, use_gpu, gpu_id, truncate_minutes):
    """Perform speech enhancement for WAV files in ``wav_dir``.

    Parameters
    ----------
    wav_files : list of str
        Paths to WAV files to enhance.

    out_dir : str
        Path to output directory for enhanced WAV files.

    use_gpu : bool, optional
        If True and GPU is available, perform all processing on GPU.
        (Default: True)

    gpu_id : int, optional
         Id of GPU on which to do computation.
         (Default: 0)

    truncate_minutes: float
        Maximimize size in minutes to process at a time. The enhancement will
        be done on chunks of audio no greather than ``truncate_minutes``
        minutes duration.
    """
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    # Load global MVN statistics.
    glo_mean_var = sio.loadmat(GLOBAL_MEAN_VAR_MATF)
    mean = glo_mean_var['global_mean']
    var = glo_mean_var['global_var']

    # Perform speech enhancement.
    for wav in wav_files:
        # Read noisy audio WAV file.
        rate, wav_data = wav_io.read(wav)
        if rate != SR:
            print('ERROR: Sample rate of file "%s" is not %d Hz. Skipping.' %
                  (wav, SR))
            continue

        # Apply peak-normalization first.
        wav_data = utils.peak_normalization(wav_data)

        # Perform denoising in chunks of size chunk_length samples.
        chunk_length = int(truncate_minutes * rate * 60)
        total_chunks = int(
            math.ceil(wav_data.size / chunk_length))
        data_se = [] # Will hold enhanced audio data for each chunk.
        for i in range(1, total_chunks + 1):
            tmp_dir = tempfile.mkdtemp()
            try:
                # Get samples for this chunk.
                bi = (i-1)*chunk_length # Index of first sample of this chunk.
                ei = bi + chunk_length # Index of last sample of this chunk + 1.
                temp = wav_data[bi:ei]
                print('Processing file: %s, segment: %d/%d.' %
                      (wav, i, total_chunks))

                # Skip denoising if chunk is too short.
                if temp.shape[0] < WL2:
                    data_se.append(temp)
                    continue

                # Determine paths to the temporary files to be created.
                noisy_normed_lps_fn = os.path.join(
                    tmp_dir, 'noisy_normed_lps.htk')
                noisy_normed_lps_scp_fn = os.path.join(
                    tmp_dir, 'noisy_normed_lps.scp')
                irm_fn = os.path.join(
                    tmp_dir, 'irm.mat')

                # Extract LPS features from waveform.
                noisy_htkdata = utils.wav2logspec(temp, window=np.hamming(WL))

                # Do MVN before decoding.
                normed_noisy = (noisy_htkdata - mean) / var

                # Write features to HTK binary format making sure to also
                # create a script file.
                utils.write_htk(
                    noisy_normed_lps_fn, normed_noisy, samp_period=SR,
                    parm_kind=9)
                cntk_len = noisy_htkdata.shape[0] - 1
                with open(noisy_normed_lps_scp_fn, 'w') as f:
                    f.write('irm=%s[0,%d]\n' % (noisy_normed_lps_fn, cntk_len))

                # Apply CNTK model to determine ideal ratio mask (IRM), which will
                # be output to the temp directory as irm.mat. In order to avoid a
                # memory leak, must do this in a separate process which we then
                # kill.
                p = Process(
                    target=decode_model,
                    args=(noisy_normed_lps_scp_fn, tmp_dir, NFREQS, use_gpu,
                          gpu_id))
                p.start()
                p.join()

                # Read in IRM and directly mask the original LPS features.
                irm = sio.loadmat(irm_fn)['IRM']
                masked_lps = noisy_htkdata + np.log(irm)

                # Reconstruct audio.
                wave_recon = utils.logspec2wav(
                    masked_lps, temp, window=np.hamming(WL), n_per_seg=WL,
                    noverlap=WL2)
                data_se.append(wave_recon)
            finally:
                shutil.rmtree(tmp_dir)
        data_se = np.concatenate(data_se)
        bn = os.path.basename(wav)
        output_wav = os.path.join(out_dir, bn)
        wav_io.write(output_wav, SR, data_se)
        print('Finished processing file "%s".' % wav)


def main():
    """Main."""
    parser = argparse.ArgumentParser(
        description='Denoise WAV files.', add_help=True)
    parser.add_argument(
        '--wav_dir', nargs=None, type=str, metavar='STR',
        help='directory containing WAV files to denoise '
             '(default: %(default)s')
    parser.add_argument(
        '--output_dir', nargs=None, type=str, metavar='STR',
        help='output directory for denoised WAV files (default: %(default)s)')
    parser.add_argument(
        '-S', dest='scpf', nargs=None, type=str, metavar='STR',
        help='script file of paths to WAV files to denosie (detault: %(default)s)')
    parser.add_argument(
        '--use_gpu', nargs=None, default='true', type=str, metavar='STR',
        choices=['true', 'false'],
        help='whether or not to use GPU (default: %(default)s)')
    parser.add_argument(
        '--gpu_id', nargs=None, default=0, type=int, metavar='INT',
        help='device id of GPU to use (default: %(default)s)')
    parser.add_argument(
        '--truncate_minutes', nargs=None, default=10, type=float,
        metavar='FLOAT',
        help='maximum chunk size in minutes (default: %(default)s)')
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)
    args = parser.parse_args()
    if not utils.xor(args.wav_dir, args.scpf):
        parser.error('Exactly one of --wav_dir and -S must be set.')
        sys.exit(1)
    use_gpu = args.use_gpu == 'true'

    # Determine files to denoise.
    if args.scpf is not None:
        wav_files = utils.load_script_file(args.scpf, '.wav')
    else:
        wav_files = utils.listdir(args.wav_dir, ext='.wav')

    # Determine output directory for denoised audio.
    if args.output_dir is None and args.wav_dir is not None:
        utils.warn('Output directory not specified. Defaulting to "%s"' %
                   args.wav_dir)
        args.output_dir = args.wav_dir

    # Perform denoising.
    main_denoising(
        wav_files, args.output_dir, use_gpu, args.gpu_id,
        args.truncate_minutes)


if __name__ == '__main__':
    main()
