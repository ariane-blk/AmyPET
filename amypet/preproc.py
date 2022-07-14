'''
Preprocessing tools for AmyPET core processes
'''

__author__      = "Pawel Markiewicz"
__copyright__   = "Copyright 2022"


import numpy as np
import os, sys, glob, shutil
import re
from datetime import datetime, timedelta
from pathlib import Path
import dcm2niix
from subprocess import run
from itertools import combinations
import urllib

from niftypet import nimpa
import spm12
import amypet
from amypet import centiloid, preproc_suvr

import logging as log

log.basicConfig(level=log.WARNING, format=nimpa.LOG_FORMAT)


#------------------------------------------------
# DEFINITIONS:
# TODO: move these to a separate file, e.g., `defs.py`

# > SUVr time window post injection and duration
suvr_twindow = dict(
    flute=[90*60,110*60, 1200],
    fbb=[90*60,110*60, 1200],
    fbp=[50*60,60*60, 600])
margin = 0.1


# tracer names 
tracer_names = dict(
    flute=['flt', 'flut', 'flute', 'flutemetamol'],
    fbb=['fbb', 'florbetaben'],
    fbp=['fbp', 'florbetapir'])

# > break time for coffee break protocol (target)
break_time = 1800

# > time margin for the 1st coffee break acquisition
breakdyn_t = (1200, 2400)

# > minimum time for the full dynamic acquisition
fulldyn_time = 3600
#------------------------------------------------




#=====================================================================
def explore_input(
        input_fldr,
        tracer=None,
        suvr_win_def=None,
        outpath=None,
        ):

    '''
    Process the input folder of amyloid PET DICOM data.
    The folder can contain two subfolders for a coffee break protocol including
    early dynamic followed by a static scan.
    The folder can also contain static or dynamic DICOM files.
    Those files can also be within a subfolder.

    Return the dictionary of (1) the list of dictionaries for each DICOM folder
    (2) list of descriptions for each DICOM folder for classification of input

    Arguments:
    - tracer:   The name of one of the three tracers: 'flute', 'fbb', 'fbp'
    - suvr_win_def: The definition of SUVr time frame (SUVr/CL is always calculated)
                as a two-element list [t_start, t_stop] in seconds.  If the 
                window is not defined the function will attempt to get the 
                information from the tracer info and use the default (as
                defined in`defs.py`)
    - outpath:  output path where all the intermediate and final results are
                stored.

    '''


    # > make the input a Path object
    input_fldr = Path(input_fldr)

    if not input_fldr.is_dir():
        raise ValueError('Incorrect input - not a folder!')

    if outpath is None:
        amyout = input_fldr.parent/'amypet_output'
    else:
        amyout = Path(outpath)
    nimpa.create_dir(amyout)




    #================================================
    # > first check if the folder is has DICOM series

    # > multiple series in folders (if any)
    msrs = []
    for itm in input_fldr.iterdir():
        if itm.is_dir():
            srs = nimpa.dcmsort(itm, grouping='a+t+d', copy_series=True, outpath=amyout)
            if srs:
                msrs.append(srs)

    # > check files in the input folder
    srs = nimpa.dcmsort(input_fldr, grouping='a+t+d', copy_series=True, outpath=amyout)
    if srs:
        msrs.append(srs)
    #================================================



    # > initialise the list of acquisition classification
    msrs_class = []

    # > time-sorted series
    msrs_t = []

    for m in msrs:

        # > for each folder do the following:

        # > time sorted series according to acquisition time
        srs_t ={k: v for k, v in sorted(m.items(), key=lambda item: item[1]['tacq'])}

        msrs_t.append(srs_t)

        #-----------------------------------------------
        # > frame timings relative to the injection time -
        #   radiopharmaceutical administration start time
        t_frms = []
        for k in srs_t:
            t0 = datetime.strptime(srs_t[k]['dstudy'] + srs_t[k]['tacq'], '%Y%m%d%H%M%S') - srs_t[k]['radio_start_time']
            t1 = datetime.strptime(srs_t[k]['dstudy'] + srs_t[k]['tacq'], '%Y%m%d%H%M%S') + srs_t[k]['frm_dur'] - srs_t[k]['radio_start_time']
            t_frms.append((t0.seconds, t1.seconds))

        t_starts = [t[0] for t in t_frms]
        t_stops = [t[1] for t in t_frms]

        # > overall acquisition duration
        acq_dur = t_frms[-1][-1] - t_frms[0][0]
        #-----------------------------------------------


        #-----------------------------------------------
        # > check if the frames qualify for static, fully dynamic or 
        # > coffee-break dynamic acquisition
        acq_type = None
        if t_frms[0][0]<1:
            if  t_frms[-1][-1]>breakdyn_t[0] and t_frms[-1][-1]<=breakdyn_t[1]:
                acq_type = 'breakdyn'
            elif t_frms[-1][-1]>=fulldyn_time:
                acq_type = 'fulldyn'
        elif t_frms[0][0]>1:
            acq_type = 'static'
        #-----------------------------------------------


        #-----------------------------------------------
        # > classify tracer if possible and if not given
        if tracer is None:
            if 'tracer' in srs_t[next(iter(srs_t))]:
                tracer_dcm = srs_t[next(iter(srs_t))]['tracer'].lower()
                for t in tracer_names:
                    for n in tracer_names[t]:
                        if n in tracer_dcm:
                            tracer = t

            # > probable tracers based on acquisition props
            if tracer is not None:
                tracer_ = [tracer]
            else:
                tracer_ = []

            if acq_type=='static':

                for t in suvr_twindow:
                    dur = suvr_twindow[t][2]
                    if acq_dur > dur*(1-margin) and acq_dur<dur*(1+margin) and t_frms[0][0]>suvr_twindow[t][0]*(1-margin):
                        tracer_.append(t)
        #-----------------------------------------------



        #-----------------------------------------------
        # > is the static acquisition covering the provided SUVr frame definition?
        if acq_type=='static':
            if (t_frms[0][0]>suvr_win_def[0]*(1-margin) and t_frms[-1][-1]<suvr_win_def[1]*(1+margin)):

                # > choose the best frames for the requested or default frame definitions
                if suvr_win_def is None:
                    suvr_win = suvr_twindow[tracer_[0]][:2]
                else:
                    suvr_win = suvr_win_def

                t0_suvr = min(t_starts, key=lambda x:abs(x-suvr_win[0]))
                t1_suvr = min(t_stops, key=lambda x:abs(x-suvr_win[1]))

                frm_0 = t_starts.index(t0_suvr)
                frm_1 = t_stops.index(t1_suvr)

                msrs_class.append(dict(
                    acq=[acq_type, 'suvr'],
                    time=(t0_suvr, t1_suvr),
                    timings=t_frms,
                    idxs=(frm_0, frm_1),
                    frms=[s for i,s in enumerate(srs_t) if i in range(frm_0, frm_1+1)],
                    ))

            else:
                log.warning('The acquisition does not cover the requested time frame!')
                
                msrs_class.append(
                    dict(
                        acq=[acq_type],
                        time=(t_starts[0], t_stops[-1]), 
                        idxs=(0, len(t_frms)-1),
                        frms=[s for i,s in enumerate(srs_t)],
                    )
                )
        #-----------------------------------------------
        elif acq_type=='breakdyn':

            t0_dyn = min(t_starts, key=lambda x:abs(x-0))
            t1_dyn = min(t_stops, key=lambda x:abs(x-break_time))

            frm_0 = t_starts.index(t0_dyn)
            frm_1 = t_stops.index(t1_dyn)

            msrs_class.append(dict(
                acq=[acq_type],
                time=(t0_dyn, t1_dyn),
                timings=t_frms,
                idxs=(frm_0, frm_1),
                frms=[s for i,s in enumerate(srs_t) if i in range(frm_0, frm_1+1)],
                ))
        #-----------------------------------------------
        elif acq_type=='fulldyn':

            t0_dyn = min(t_starts, key=lambda x:abs(x-0))
            t1_dyn = min(t_stops, key=lambda x:abs(x-fulldyn_time))

            frm_0 = t_starts.index(t0_dyn)
            frm_1 = t_stops.index(t1_dyn)

            msrs_class.append(dict(
                acq=[acq_type],
                time=(t0_dyn, t1_dyn),
                timings=t_frms,
                idxs=(frm_0, frm_1),
                frms=[s for i,s in enumerate(srs_t) if i in range(frm_0, frm_1+1)],
                ))
        #-----------------------------------------------



    return dict(series=msrs_t, descr=msrs_class, outpath=amyout)




#=====================================================================
def align_suvr(
        suvr_tdata,
        suvr_descr,
        outpath=None,
        reg_costfun='nmi',
        reg_force=False,
        reg_fwhm=8,
        ):
    '''
    Align SUVr frames after conversion to NIfTI format.

    Arguments:
    - reg_constfun: the cost function used in SPM registration/alignment of frames
    - reg_force:    force running the registration even if the registration results
                are already calculated and stored in the output folder.
    - reg_fwhm: the FWHM of the Gaussian kernel used for smoothing the images before
                registration and only for registration purposes.

    '''

    if outpath is None:
        align_out = suvr_tdata[next(iter(suvr_tdata))]['files'][0].parent.parent
    else:
        align_out = Path(outpath)


    # > NIfTI output folder
    niidir = align_out/f'NIfTI_SUVr'
    nimpa.create_dir(niidir)

    # > folder of resampled and aligned NIfTI files (SPM)
    rsmpl_opth = niidir/'SPM-aligned'
    nimpa.create_dir(rsmpl_opth)

    # > the name of the output re-aligned file name
    faligned = 'SUVr_aligned_'+nimpa.rem_chars(suvr_tdata[next(iter(suvr_tdata))]['series'])+'.nii.gz'
    faligned = niidir/faligned

    # > check if the file exists
    if reg_force or not faligned.is_file():

        # > remove any files from previous runs
        files = niidir.glob('*')
        for f in files:
            if f.is_file():
                os.remove(f)
            else:
                shutil.rmtree(f)

        # > output nifty frame files
        nii_frms = []

        #-----------------------------------------------
        # > convert the individual DICOM frames to NIfTI
        for i,k in enumerate(suvr_descr['frms']):

            run([dcm2niix.bin,
                 '-i', 'y',
                 '-v', 'n',
                 '-o', niidir,
                 'f', '%f_%s',
                 suvr_tdata[k]['files'][0].parent])

            # > get the converted NIfTI file
            fnii = [f for f in niidir.glob('{}*.nii*'.format(suvr_tdata[k]['tacq']))]
            if len(fnii)!=1:
                raise ValueError('Unexpected number of converted NIfTI files')
            else:
                nii_frms.append(fnii[0])
        #-----------------------------------------------


        #-----------------------------------------------
        # > CORE ALIGNMENT OF SUVR FRAMES:

        # > frame-based motion metric (rotations+translation) 
        R = np.zeros( (len(nii_frms), len(nii_frms)), dtype=np.float32 )

        # > paths to the affine files
        S = [[None for _ in range(len(nii_frms))] for _ in range(len(nii_frms))]

        # > go through all possible combinations of frame registration
        for c in combinations(suvr_descr['frms'], 2):
            frm0 = suvr_descr['frms'].index(c[0])
            frm1 = suvr_descr['frms'].index(c[1])

            fnii0 = nii_frms[frm0]
            fnii1 = nii_frms[frm1]

            log.info(f'registration of frame #{frm0} and frame #{frm1}')

            # > one way registration
            spm_res = nimpa.coreg_spm(
                fnii0,
                fnii1,
                fwhm_ref = reg_fwhm,
                fwhm_flo = reg_fwhm,
                fwhm = [13,13],
                costfun=reg_costfun,
                fcomment = f'_combi_{frm0}-{frm1}',
                outpath = fnii0.parent,
                visual = 0,
                save_arr = False,
                del_uncmpr=True)

            S[frm0][frm1] = spm_res['faff']

            rot_ss = np.sum((180*spm_res['rotations']/np.pi)**2)**.5
            trn_ss = np.sum(spm_res['translations']**2)**.5
            R[frm0,frm1] = rot_ss+trn_ss

            # > the other way registration
            spm_res = nimpa.coreg_spm(
                fnii1,
                fnii0,
                fwhm_ref = reg_fwhm,
                fwhm_flo = reg_fwhm,
                fwhm = [13,13],
                costfun=reg_costfun,
                fcomment = f'_combi_{frm1}-{frm0}',
                outpath = fnii0.parent,
                visual = 0,
                save_arr = False,
                del_uncmpr=True)

            S[frm1][frm0] = spm_res['faff']

            rot_ss = np.sum((180*spm_res['rotations']/np.pi)**2)**.5
            trn_ss = np.sum(spm_res['translations']**2)**.5
            R[frm1,frm0] = rot_ss+trn_ss


        # > sum frames along floating frames
        fsum = np.sum(R,axis=0)

        # > sum frames along reference frames
        rsum = np.sum(R,axis=1)

        # > reference frame for SUVr composite frame
        rfrm = np.argmin(fsum+rsum)

        niiref = nimpa.getnii(nii_frms[rfrm], output='all')

        # > initialise target aligned SUVr image
        niiim = np.zeros((len(nii_frms),)+niiref['shape'], dtype=np.float32)

        # > copy in the target frame for SUVr composite
        niiim[rfrm,...] = niiref['im']

        for ifrm in range(len(nii_frms)):
            if ifrm==rfrm: continue

            #> resample images for alignment
            frsmpl = nimpa.resample_spm(
                    nii_frms[rfrm],
                    nii_frms[ifrm],
                    S[rfrm][ifrm],
                    intrp = 1.,
                    outpath=rsmpl_opth,
                    pickname='flo',
                    del_ref_uncmpr = True,
                    del_flo_uncmpr = True,
                    del_out_uncmpr = True,
                )

            niiim[ifrm,...] = nimpa.getnii(frsmpl)


        # > save aligned SUVr frames
        nimpa.array2nii(
            niiim,
            niiref['affine'],
            faligned,
            descrip='AmyPET: aligned SUVr frames',
            trnsp = (niiref['transpose'].index(0),
                     niiref['transpose'].index(1),
                     niiref['transpose'].index(2)),
            flip = niiref['flip'])
        #-----------------------------------------------


    return dict(fpet=faligned, Metric=R, faff=S, outpath=niidir)

#=====================================================================