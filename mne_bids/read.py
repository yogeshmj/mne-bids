"""Check whether a file format is supported by BIDS and then load it."""
# Authors: Mainak Jas <mainak.jas@telecom-paristech.fr>
#          Alexandre Gramfort <alexandre.gramfort@telecom-paristech.fr>
#          Teon Brooks <teon.brooks@gmail.com>
#          Chris Holdgraf <choldgraf@berkeley.edu>
#          Stefan Appelhoff <stefan.appelhoff@mailbox.org>
#
# License: BSD-3-Clause
import os.path as op
from pathlib import Path
import glob
import json
import re
import warnings
from datetime import datetime, timezone

import numpy as np
import mne
from mne import io, read_events, events_from_annotations
from mne.io.pick import pick_channels_regexp
from mne.utils import has_nibabel, logger, warn, get_subjects_dir
from mne.coreg import fit_matched_points
from mne.transforms import apply_trans

from mne_bids.dig import _read_dig_bids
from mne_bids.tsv_handler import _from_tsv, _drop
from mne_bids.config import (ALLOWED_DATATYPE_EXTENSIONS,
                             ANNOTATIONS_TO_KEEP,
                             reader, _map_options)
from mne_bids.utils import _extract_landmarks, _get_ch_type_mapping
from mne_bids.path import (BIDSPath, _parse_ext, _find_matching_sidecar,
                           _infer_datatype)


def _read_raw(raw_fpath, electrode=None, hsp=None, hpi=None,
              allow_maxshield=False, config=None, verbose=None, **kwargs):
    """Read a raw file into MNE, making inferences based on extension."""
    _, ext = _parse_ext(raw_fpath)

    # KIT systems
    if ext in ['.con', '.sqd']:
        raw = io.read_raw_kit(raw_fpath, elp=electrode, hsp=hsp,
                              mrk=hpi, preload=False, **kwargs)

    # BTi systems
    elif ext == '.pdf':
        raw = io.read_raw_bti(raw_fpath, config_fname=config,
                              head_shape_fname=hsp,
                              preload=False, verbose=verbose,
                              **kwargs)

    elif ext == '.fif':
        raw = reader[ext](raw_fpath, allow_maxshield, **kwargs)

    elif ext in ['.ds', '.vhdr', '.set', '.edf', '.bdf']:
        raw = reader[ext](raw_fpath, **kwargs)

    # MEF and NWB are allowed, but not yet implemented
    elif ext in ['.mef', '.nwb']:
        raise ValueError(f'Got "{ext}" as extension. This is an allowed '
                         f'extension but there is no IO support for this '
                         f'file format yet.')

    # No supported data found ...
    # ---------------------------
    else:
        raise ValueError(f'Raw file name extension must be one '
                         f'of {ALLOWED_DATATYPE_EXTENSIONS}\n'
                         f'Got {ext}')
    return raw


def _read_events(events_data, event_id, raw, task=None, verbose=None):
    """Retrieve events (for use in *_events.tsv) from FIFF/array & Annotations.

    Parameters
    ----------
    events_data : str | np.ndarray | None
        If a string, a path to an events file. If an array, an MNE events array
        (shape n_events, 3). If None, events will be generated from
        ``raw.annotations``.
    event_id : dict | None
        The event id dict used to create a 'trial_type' column in events.tsv,
        mapping a description key to an integer-valued event code.
    raw : mne.io.Raw
        The data as MNE-Python Raw object.
    task : str | None
        If task may be resting state, silence warnings.
    verbose : bool | str | int | None
        If not None, override default verbose level (see :func:`mne.verbose`).

    Returns
    -------
    all_events : np.ndarray, shape = (n_events, 3)
        The first column contains the event time in samples and the third
        column contains the event id. The second column is ignored for now but
        typically contains the value of the trigger channel either immediately
        before the event or immediately after.
    all_dur : np.ndarray, shape (n_events,)
        The event durations in seconds.
    all_desc : dict
        A dictionary with the keys corresponding to the event descriptions and
        the values to the event IDs.

    """
    # get events from events_data
    if isinstance(events_data, str):
        events = read_events(events_data, verbose=verbose).astype(int)
    elif isinstance(events_data, np.ndarray):
        if events_data.ndim != 2:
            raise ValueError('Events must have two dimensions, '
                             f'found {events_data.ndim}')
        if events_data.shape[1] != 3:
            raise ValueError('Events must have second dimension of length 3, '
                             f'found {events_data.shape[1]}')
        events = events_data
    else:
        events = np.empty(shape=(0, 3), dtype=int)

    if events.size > 0:
        # Only keep events for which we have an ID <> description mapping.
        ids_without_desc = set(events[:, 2]) - set(event_id.values())
        if ids_without_desc:
            raise ValueError(
                f'No description was specified for the following event(s): '
                f'{", ".join([str(x) for x in sorted(ids_without_desc)])}. '
                f'Please add them to the event_id dictionary, or drop them '
                f'from the events_data array.'
            )
        del ids_without_desc
        mask = [e in list(event_id.values()) for e in events[:, 2]]
        events = events[mask]

        # Append events to raw.annotations. All event onsets are relative to
        # measurement beginning.
        id_to_desc_map = dict(zip(event_id.values(), event_id.keys()))
        # We don't pass `first_samp`, as set_annotations() below will take
        # care of this shift automatically.
        new_annotations = mne.annotations_from_events(
            events=events, sfreq=raw.info['sfreq'], event_desc=id_to_desc_map,
            orig_time=raw.annotations.orig_time, verbose=verbose)

        raw = raw.copy()  # Don't alter the original.
        annotations = raw.annotations.copy()

        # We use `+=` here because `Annotations.__iadd__()` does the right
        # thing and also performs a sanity check on `Annotations.orig_time`.
        annotations += new_annotations
        raw.set_annotations(annotations)
        del id_to_desc_map, annotations, new_annotations

    # Now convert the Annotations to events.
    all_events, all_desc = events_from_annotations(
        raw,
        event_id=event_id,
        regexp=None,  # Include `BAD_` and `EDGE_` Annotations, too.
        verbose=verbose
    )
    all_dur = raw.annotations.duration

    # warn no events if not rest
    if (all_events.size == 0 and task is not None and
            not task.startswith('rest')):
        warn('No events found or provided. Please add annotations to the raw '
             'data, or provide the events_data and event_id parameters. For '
             'resting state data, BIDS recommends naming the task using '
             'labels beginning with "rest".')

    return all_events, all_dur, all_desc


def _handle_participants_reading(participants_fname, raw,
                                 subject, verbose=None):
    participants_tsv = _from_tsv(participants_fname)
    subjects = participants_tsv['participant_id']
    row_ind = subjects.index(subject)

    # set data from participants tsv into subject_info
    for col_name, value in participants_tsv.items():
        if col_name == 'sex' or col_name == 'hand':
            value = _map_options(what=col_name, key=value[row_ind],
                                 fro='bids', to='mne')
            # We don't know how to translate to MNE, so skip.
            if value is None:
                if col_name == 'sex':
                    info_str = 'subject sex'
                else:
                    info_str = 'subject handedness'
                warn(f'Unable to map `{col_name}` value to MNE. '
                     f'Not setting {info_str}.')
        else:
            value = value[row_ind]
        # add data into raw.Info
        if raw.info['subject_info'] is None:
            raw.info['subject_info'] = dict()
        key = 'his_id' if col_name == 'participant_id' else col_name
        raw.info['subject_info'][key] = value

    return raw


def _handle_scans_reading(scans_fname, raw, bids_path, verbose=False):
    """Read associated scans.tsv and set meas_date."""
    scans_tsv = _from_tsv(scans_fname)
    fname = bids_path.fpath.name

    if fname.endswith('.pdf'):
        # for BTI files, the scan is an entire directory
        fname = fname.split('.')[0]

    # get the row corresponding to the file
    # use string concatenation instead of os.path
    # to work nicely with windows
    data_fname = bids_path.datatype + '/' + fname
    fnames = scans_tsv['filename']
    if 'acq_time' in scans_tsv:
        acq_times = scans_tsv['acq_time']
    else:
        acq_times = ['n/a'] * len(fnames)
    row_ind = fnames.index(data_fname)

    # check whether all split files have the same acq_time
    # and throw an error if they don't
    if '_split-' in fname:
        split_idx = fname.find('split-')
        pattern = re.compile(bids_path.datatype + '/' +
                             bids_path.basename[:split_idx] +
                             r'split-\d+_' + bids_path.datatype +
                             bids_path.fpath.suffix)
        split_fnames = list(filter(pattern.match, fnames))
        split_acq_times = []
        for split_f in split_fnames:
            split_acq_times.append(acq_times[fnames.index(split_f)])
        if len(set(split_acq_times)) != 1:
            raise ValueError("Split files must have the same acq_time.")

    # extract the acquisition time from scans file
    acq_time = acq_times[row_ind]
    if acq_time != 'n/a':
        # microseconds in the acquisition time is optional
        if '.' not in acq_time:
            # acquisition time ends with '.%fZ' microseconds string
            acq_time += '.0Z'
        acq_time = datetime.strptime(acq_time, '%Y-%m-%dT%H:%M:%S.%fZ')
        acq_time = acq_time.replace(tzinfo=timezone.utc)

        if verbose:
            logger.debug(f'Loaded {scans_fname} scans file to set '
                         f'acq_time as {acq_time}.')
        # First set measurement date to None and then call call anonymize() to
        # remove any traces of the measurement date we wish
        # to replace – it might lurk out in more places than just
        # raw.info['meas_date'], e.g. in info['meas_id]['secs'] and in
        # info['file_id'], which are not affected by set_meas_date().
        # The combined use of set_meas_date(None) and anonymize() is suggested
        # by the MNE documentation, and in fact we cannot load e.g. OpenNeuro
        # ds003392 without this combination.
        raw.set_meas_date(None)
        with warnings.catch_warnings():
            # This is to silence a warning emitted by MNE-Python < 0.24. The
            # warnings filter can be safely removed once we drop support for
            # MNE-Python 0.23 and older.
            warnings.filterwarnings(
                action='ignore',
                message="Input info has 'meas_date' set to None",
                category=RuntimeWarning,
                module='mne'
            )
            raw.anonymize(daysback=None, keep_his=True)
        raw.set_meas_date(acq_time)

    return raw


def _handle_info_reading(sidecar_fname, raw, verbose=None):
    """Read associated sidecar JSON and populate raw.

    Handle PowerLineFrequency of recording.
    """
    with open(sidecar_fname, 'r', encoding='utf-8-sig') as fin:
        sidecar_json = json.load(fin)

    # read in the sidecar JSON's line frequency
    line_freq = sidecar_json.get("PowerLineFrequency")
    if line_freq == "n/a":
        line_freq = None

    if raw.info["line_freq"] is not None and line_freq is None:
        line_freq = raw.info["line_freq"]  # take from file is present

    if raw.info["line_freq"] is not None and line_freq is not None:
        # if both have a set Power Line Frequency, then
        # check that they are the same, else there is a
        # discrepancy in the metadata of the dataset.
        if raw.info["line_freq"] != line_freq:
            raise ValueError("Line frequency in sidecar json does "
                             "not match the info datastructure of "
                             "the mne.Raw. "
                             "Raw is -> {} ".format(raw.info["line_freq"]),
                             "Sidecar JSON is -> {} ".format(line_freq))

    raw.info["line_freq"] = line_freq

    # get cHPI info
    chpi = sidecar_json.get('ContinuousHeadLocalization')
    if chpi is None:
        # no cHPI info in the sidecar – leave raw.info unchanged
        pass
    elif chpi is True:
        from mne.io.ctf import RawCTF
        from mne.io.kit.kit import RawKIT

        if isinstance(raw, RawCTF):
            # Pick channels corresponding to the cHPI positions
            hpi_picks = pick_channels_regexp(raw.info['ch_names'],
                                             'HLC00[123][123].*')
            if len(hpi_picks) != 9:
                raise ValueError(
                    f'Could not find all cHPI channels that we expected for '
                    f'CTF data. Expected: 9, found: {len(hpi_picks)}'
                )
            logger.info('Cannot verify that the cHPI frequencies provided in '
                        'the MEG JSON sidecar file correspond to the raw data '
                        'for CTF files.')
        elif isinstance(raw, RawKIT):
            logger.info('Cannot verify that the cHPI information provided in '
                        'the MEG JSON sidecar file corresponds to the raw '
                        'data for KIT files.')
        else:
            hpi_freqs_json = sidecar_json['HeadCoilFrequency']
            try:
                hpi_freqs_raw, _, _ = mne.chpi.get_chpi_info(raw.info)
            except ValueError:
                logger.info(
                    'Cannot verify that the cHPI frequencies provided in '
                    'the MEG JSON sidecar file correspond to those in the '
                    'raw data. (Was it converted from another format?)'
                )
            else:
                if not np.allclose(hpi_freqs_json, hpi_freqs_raw):
                    raise ValueError(
                        f'The cHPI coil frequencies in the sidecar file '
                        f'{sidecar_fname}:\n    {hpi_freqs_json}\ndiffer from'
                        f' what is stored in the raw data:\n'
                        f'    {hpi_freqs_raw}\nCannot proceed.'
                    )
    else:
        if raw.info['hpi_subsystem']:
            logger.info('Dropping cHPI information stored in raw data, '
                        'following specification in sidecar file')
        raw.info['hpi_subsystem'] = None
        raw.info['hpi_meas'] = []

    return raw


def _handle_events_reading(events_fname, raw):
    """Read associated events.tsv and populate raw.

    Handle onset, duration, and description of each event.
    """
    logger.info('Reading events from {}.'.format(events_fname))
    events_dict = _from_tsv(events_fname)

    # Get the descriptions of the events
    if 'trial_type' in events_dict:
        trial_type_col_name = 'trial_type'
    elif 'stim_type' in events_dict:  # Backward-compat with old datasets.
        trial_type_col_name = 'stim_type'
        warn(f'The events file, {events_fname}, contains a "stim_type" '
             f'column. This column should be renamed to "trial_type" for '
             f'BIDS compatibility.')
    else:
        trial_type_col_name = None

    if trial_type_col_name is not None:
        # Drop events unrelated to a trial type
        events_dict = _drop(events_dict, 'n/a', trial_type_col_name)

        if 'value' in events_dict:
            # Check whether the `trial_type` <> `value` mapping is unique.
            trial_types = events_dict[trial_type_col_name]
            values = np.asarray(events_dict['value'], dtype=str)
            for trial_type in np.unique(trial_types):
                idx = np.where(trial_type == np.atleast_1d(trial_types))[0]
                matching_values = values[idx]

                if len(np.unique(matching_values)) > 1:
                    # Event type descriptors are ambiguous; create hierarchical
                    # event descriptors.
                    logger.info(
                        f'The event "{trial_type}" refers to multiple event '
                        f'values. Creating hierarchical event names.')
                    for ii in idx:
                        new_name = f'{trial_type}/{values[ii]}'
                        logger.info(f'    Renaming event: {trial_type} -> '
                                    f'{new_name}')
                        trial_types[ii] = new_name
            descriptions = np.asarray(trial_types, dtype=str)
        else:
            descriptions = np.asarray(events_dict[trial_type_col_name],
                                      dtype=str)
    elif 'value' in events_dict:
        # If we don't have a proper description of the events, perhaps we have
        # at least an event value?
        # Drop events unrelated to value
        events_dict = _drop(events_dict, 'n/a', 'value')
        descriptions = np.asarray(events_dict['value'], dtype=str)

    # Worst case, we go with 'n/a' for all events
    else:
        descriptions = np.array(['n/a'] * len(events_dict['onset']), dtype=str)

    # Deal with "n/a" strings before converting to float
    onsets = np.array(
        [np.nan if on == 'n/a' else on for on in events_dict['onset']],
        dtype=float)
    durations = np.array(
        [0 if du == 'n/a' else du for du in events_dict['duration']],
        dtype=float)

    # Keep only events where onset is known
    good_events_idx = ~np.isnan(onsets)
    onsets = onsets[good_events_idx]
    durations = durations[good_events_idx]
    descriptions = descriptions[good_events_idx]
    del good_events_idx

    # Add events as Annotations, but keep essential Annotations present in
    # raw file
    annot_from_raw = raw.annotations.copy()

    annot_from_events = mne.Annotations(onset=onsets,
                                        duration=durations,
                                        description=descriptions)
    raw.set_annotations(annot_from_events)

    annot_idx_to_keep = [idx for idx, annot in enumerate(annot_from_raw)
                         if annot['description'] in ANNOTATIONS_TO_KEEP]
    annot_to_keep = annot_from_raw[annot_idx_to_keep]

    if len(annot_to_keep):
        raw.set_annotations(raw.annotations + annot_to_keep)

    return raw


def _get_bads_from_tsv_data(tsv_data):
    """Extract names of bads from data read from channels.tsv."""
    idx = []
    for ch_idx, status in enumerate(tsv_data['status']):
        if status.lower() == 'bad':
            idx.append(ch_idx)

    bads = [tsv_data['name'][i] for i in idx]
    return bads


def _handle_channels_reading(channels_fname, raw):
    """Read associated channels.tsv and populate raw.

    Updates status (bad) and types of channels.
    """
    logger.info('Reading channel info from {}.'.format(channels_fname))
    channels_dict = _from_tsv(channels_fname)
    ch_names_tsv = channels_dict['name']

    # Now we can do some work.
    # The "type" column is mandatory in BIDS. We can use it to set channel
    # types in the raw data using a mapping between channel types
    channel_type_dict = dict()

    # Get the best mapping we currently have from BIDS to MNE nomenclature
    bids_to_mne_ch_types = _get_ch_type_mapping(fro='bids', to='mne')
    ch_types_json = channels_dict['type']
    for ch_name, ch_type in zip(ch_names_tsv, ch_types_json):

        # Try to map from BIDS nomenclature to MNE, leave channel type
        # untouched if we are uncertain
        updated_ch_type = bids_to_mne_ch_types.get(ch_type, None)

        if updated_ch_type is None:
            # XXX Try again with uppercase spelling – this should be removed
            # XXX once https://github.com/bids-standard/bids-validator/issues/1018  # noqa:E501
            # XXX has been resolved.
            # XXX x-ref https://github.com/mne-tools/mne-bids/issues/481
            updated_ch_type = bids_to_mne_ch_types.get(ch_type.upper(), None)
            if updated_ch_type is not None:
                msg = ('The BIDS dataset contains channel types in lowercase '
                       'spelling. This violates the BIDS specification and '
                       'will raise an error in the future.')
                warn(msg)

        if updated_ch_type is not None:
            channel_type_dict[ch_name] = updated_ch_type

    # Special handling for (synthesized) stimulus channel
    synthesized_stim_ch_name = 'STI 014'
    if (synthesized_stim_ch_name in raw.ch_names and
            synthesized_stim_ch_name not in ch_names_tsv):
        logger.info(
            f'The stimulus channel "{synthesized_stim_ch_name}" is present in '
            f'the raw data, but not included in channels.tsv. Removing the '
            f'channel.')
        raw.drop_channels([synthesized_stim_ch_name])

    # Rename channels in loaded Raw to match those read from the BIDS sidecar
    if len(ch_names_tsv) != len(raw.ch_names):
        warn(f'The number of channels in the channels.tsv sidecar file '
             f'({len(ch_names_tsv)}) does not match the number of channels '
             f'in the raw data file ({len(raw.ch_names)}). Will not try to '
             f'set channel names.')
    else:
        for bids_ch_name, raw_ch_name in zip(ch_names_tsv,
                                             raw.ch_names.copy()):
            if bids_ch_name != raw_ch_name:
                raw.rename_channels({raw_ch_name: bids_ch_name})

    # Set the channel types in the raw data according to channels.tsv
    ch_type_map_avail = {
        ch_name: ch_type
        for ch_name, ch_type in channel_type_dict.items()
        if ch_name in raw.ch_names
    }
    ch_diff = set(channel_type_dict.keys()) - set(ch_type_map_avail.keys())
    if ch_diff:
        warn(f'Cannot set channel type for the following channels, as they '
             f'are missing in the raw data: {", ".join(sorted(ch_diff))}')
    raw.set_channel_types(ch_type_map_avail)

    # Set bad channels based on _channels.tsv sidecar
    if 'status' in channels_dict:
        bads_tsv = _get_bads_from_tsv_data(channels_dict)
        bads_avail = [ch_name for ch_name in bads_tsv
                      if ch_name in raw.ch_names]

        ch_diff = set(bads_tsv) - set(bads_avail)
        if ch_diff:
            warn(f'Cannot set "bad" status for the following channels, as '
                 f'they are missing in the raw data: '
                 f'{", ".join(sorted(ch_diff))}')

        raw.info['bads'] = bads_avail

    return raw


def read_raw_bids(bids_path, extra_params=None, verbose=True):
    """Read BIDS compatible data.

    Will attempt to read associated events.tsv and channels.tsv files to
    populate the returned raw object with raw.annotations and raw.info['bads'].

    Parameters
    ----------
    bids_path : mne_bids.BIDSPath
        The file to read. The :class:`mne_bids.BIDSPath` instance passed here
        **must** have the ``.root`` attribute set. The ``.datatype`` attribute
        **may** be set. If ``.datatype`` is not set and only one data type
        (e.g., only EEG or MEG data) is present in the dataset, it will be
        selected automatically.

        .. note::
           If ``bids_path`` points to a symbolic link of a ``.fif`` file
           without a ``split`` entity, the link will be resolved before
           reading.

    extra_params : None | dict
        Extra parameters to be passed to MNE read_raw_* functions.
        Note that the ``exclude`` parameter, which is supported by some
        MNE-Python readers, is not supported; instead, you need to subset
        your channels **after** reading.
    verbose : bool
        The verbosity level.

    Returns
    -------
    raw : mne.io.Raw
        The data as MNE-Python Raw object.

    Raises
    ------
    RuntimeError
        If multiple recording data types are present in the dataset, but
        ``datatype=None``.

    RuntimeError
        If more than one data files exist for the specified recording.

    RuntimeError
        If no data file in a supported format can be located.

    ValueError
        If the specified ``datatype`` cannot be found in the dataset.

    """
    if not isinstance(bids_path, BIDSPath):
        raise RuntimeError('"bids_path" must be a BIDSPath object. Please '
                           'instantiate using mne_bids.BIDSPath().')

    bids_path = bids_path.copy()
    sub = bids_path.subject
    ses = bids_path.session
    bids_root = bids_path.root
    datatype = bids_path.datatype
    suffix = bids_path.suffix

    # check root available
    if bids_root is None:
        raise ValueError('The root of the "bids_path" must be set. '
                         'Please use `bids_path.update(root="<root>")` '
                         'to set the root of the BIDS folder to read.')

    # infer the datatype and suffix if they are not present in the BIDSPath
    if datatype is None:
        datatype = _infer_datatype(root=bids_root, sub=sub, ses=ses)
        bids_path.update(datatype=datatype)
    if suffix is None:
        bids_path.update(suffix=datatype)

    data_dir = bids_path.directory
    bids_fname = bids_path.fpath.name

    if op.splitext(bids_fname)[1] == '.pdf':
        bids_raw_folder = op.join(data_dir, f'{bids_path.basename}')
        bids_fpath = glob.glob(op.join(bids_raw_folder, 'c,rf*'))[0]
        config = op.join(bids_raw_folder, 'config')
    else:
        bids_fpath = op.join(data_dir, bids_fname)
        # Resolve for FIFF files
        if (bids_fpath.endswith('.fif') and bids_path.split is None and
                op.islink(bids_fpath)):
            target_path = op.realpath(bids_fpath)
            logger.info(f'Resolving symbolic link: '
                        f'{bids_fpath} -> {target_path}')
            bids_fpath = target_path
        config = None

    if extra_params is None:
        extra_params = dict()
    elif 'exclude' in extra_params:
        del extra_params['exclude']
        logger.info('"exclude" parameter is not supported by read_raw_bids')

    if bids_fname.endswith('.fif') and 'allow_maxshield' not in extra_params:
        extra_params['allow_maxshield'] = True

    raw = _read_raw(bids_fpath, electrode=None, hsp=None, hpi=None,
                    config=config, verbose=None, **extra_params)

    # Try to find an associated events.tsv to get information about the
    # events in the recorded data
    events_fname = _find_matching_sidecar(bids_path, suffix='events',
                                          extension='.tsv',
                                          on_error='warn')
    if events_fname is not None:
        raw = _handle_events_reading(events_fname, raw)

    # Try to find an associated channels.tsv to get information about the
    # status and type of present channels
    channels_fname = _find_matching_sidecar(bids_path,
                                            suffix='channels',
                                            extension='.tsv',
                                            on_error='warn')
    if channels_fname is not None:
        raw = _handle_channels_reading(channels_fname, raw)

    # Try to find an associated electrodes.tsv and coordsystem.json
    # to get information about the status and type of present channels
    on_error = 'warn' if suffix == 'ieeg' else 'ignore'
    electrodes_fname = _find_matching_sidecar(bids_path,
                                              suffix='electrodes',
                                              extension='.tsv',
                                              on_error=on_error)
    coordsystem_fname = _find_matching_sidecar(bids_path,
                                               suffix='coordsystem',
                                               extension='.json',
                                               on_error=on_error)
    if electrodes_fname is not None:
        if coordsystem_fname is None:
            raise RuntimeError(f"BIDS mandates that the coordsystem.json "
                               f"should exist if electrodes.tsv does. "
                               f"Please create coordsystem.json for"
                               f"{bids_path.basename}")
        if datatype in ['meg', 'eeg', 'ieeg']:
            _read_dig_bids(electrodes_fname, coordsystem_fname,
                           raw=raw, datatype=datatype, verbose=verbose)

    # Try to find an associated sidecar .json to get information about the
    # recording snapshot
    sidecar_fname = _find_matching_sidecar(bids_path,
                                           suffix=datatype,
                                           extension='.json',
                                           on_error='warn')
    if sidecar_fname is not None:
        raw = _handle_info_reading(sidecar_fname, raw, verbose=verbose)

    # read in associated scans filename
    scans_fname = BIDSPath(
        subject=bids_path.subject, session=bids_path.session,
        suffix='scans', extension='.tsv',
        root=bids_path.root
    ).fpath

    if scans_fname.exists():
        raw = _handle_scans_reading(scans_fname, raw, bids_path,
                                    verbose=verbose)

    # read in associated subject info from participants.tsv
    participants_tsv_fpath = op.join(bids_root, 'participants.tsv')
    subject = f"sub-{bids_path.subject}"
    if op.exists(participants_tsv_fpath):
        raw = _handle_participants_reading(participants_tsv_fpath, raw,
                                           subject, verbose=verbose)
    else:
        warn("Participants file not found for {}... Not reading "
             "in any particpants.tsv data.".format(bids_fname))

    assert raw.annotations.orig_time == raw.info['meas_date']
    return raw


def get_head_mri_trans(bids_path, extra_params=None, t1_bids_path=None,
                       fs_subject=None, fs_subjects_dir=None):
    """Produce transformation matrix from MEG and MRI landmark points.

    Will attempt to read the landmarks of Nasion, LPA, and RPA from the sidecar
    files of (i) the MEG and (ii) the T1-weighted MRI data. The two sets of
    points will then be used to calculate a transformation matrix from head
    coordinates to MRI coordinates.

    .. note:: The MEG and MRI data need **not** necessarily be stored in the
              same session or even in the same BIDS dataset. See the
              ``t1_bids_path`` parameter for details.

    Parameters
    ----------
    bids_path : mne_bids.BIDSPath
        The path of the electrophysiology recording.
    extra_params : None | dict
        Extra parameters to be passed to :func:`mne.io.read_raw` when reading
        the MEG file.
    t1_bids_path : mne_bids.BIDSPath | None
        If ``None`` (default), will try to discover the T1-weighted MRI file
        based on the name and location of the MEG recording specified via the
        ``bids_path`` parameter. Alternatively, you explicitly specify which
        T1-weighted MRI scan to use for extraction of MRI landmarks. To do
        that, pass a :class:`mne_bids.BIDSPath` pointing to the scan.
        Use this parameter e.g. if the T1 scan was recorded during a different
        session than the MEG. It is even possible to point to a T1 image stored
        in an entirely different BIDS dataset than the MEG data.
    fs_subject : str | None
        The subject identifier used for FreeSurfer. If ``None``, defaults to
        the ``subject`` entity in ``bids_path``.
    fs_subjects_dir : str | pathlib.Path | None
        The FreeSurfer subjects directory. If ``None``, defaults to the
        ``SUBJECTS_DIR`` environment variable.

        .. versionadded:: 0.8

    Returns
    -------
    trans : mne.transforms.Transform
        The data transformation matrix from head to MRI coordinates.
    """
    if not has_nibabel():  # pragma: no cover
        raise ImportError('This function requires nibabel.')
    import nibabel as nib

    if not isinstance(bids_path, BIDSPath):
        raise RuntimeError('"bids_path" must be a BIDSPath object. Please '
                           'instantiate using mne_bids.BIDSPath().')

    # check root available
    meg_bids_path = bids_path.copy()
    del bids_path
    if meg_bids_path.root is None:
        raise ValueError('The root of the "bids_path" must be set. '
                         'Please use `bids_path.update(root="<root>")` '
                         'to set the root of the BIDS folder to read.')

    # only get this for MEG data
    meg_bids_path.update(datatype='meg', suffix='meg')

    # Get the sidecar file for MRI landmarks
    match_bids_path = meg_bids_path if t1_bids_path is None else t1_bids_path
    t1w_path = _find_matching_sidecar(
        match_bids_path, suffix='T1w', extension='.nii.gz')
    t1w_json_path = _find_matching_sidecar(
        match_bids_path, suffix='T1w', extension='.json')

    # Get MRI landmarks from the JSON sidecar
    with open(t1w_json_path, 'r', encoding='utf-8') as f:
        t1w_json = json.load(f)
    mri_coords_dict = t1w_json.get('AnatomicalLandmarkCoordinates', dict())

    # landmarks array: rows: [LPA, NAS, RPA]; columns: [x, y, z]
    mri_landmarks = np.full((3, 3), np.nan)
    for landmark_name, coords in mri_coords_dict.items():
        if landmark_name.upper() == 'LPA':
            mri_landmarks[0, :] = coords
        elif landmark_name.upper() == 'RPA':
            mri_landmarks[2, :] = coords
        elif (landmark_name.upper() == 'NAS' or
              landmark_name.lower() == 'nasion'):
            mri_landmarks[1, :] = coords
        else:
            continue

    if np.isnan(mri_landmarks).any():
        raise RuntimeError(
            f'Could not extract fiducial points from T1w sidecar file: '
            f'{t1w_json_path}\n\n'
            f'The sidecar file SHOULD contain a key '
            f'"AnatomicalLandmarkCoordinates" pointing to an '
            f'object with the keys "LPA", "NAS", and "RPA". '
            f'Yet, the following structure was found:\n\n'
            f'{mri_coords_dict}'
        )

    # The MRI landmarks are in "voxels". We need to convert the to the
    # neuromag RAS coordinate system in order to compare the with MEG landmarks
    # see also: `mne_bids.write.write_anat`
    if fs_subject is None:
        fs_subject = f'sub-{meg_bids_path.subject}'
    fs_subjects_dir = get_subjects_dir(fs_subjects_dir, raise_error=False)
    fs_t1_fname = Path(fs_subjects_dir) / fs_subject / 'mri' / 'T1.mgz'
    if not fs_t1_fname.exists():
        raise ValueError(
            f"Could not find {fs_t1_fname}. Consider running FreeSurfer's "
            f"'recon-all` for subject {fs_subject}.")
    fs_t1_mgh = nib.load(str(fs_t1_fname))
    t1_nifti = nib.load(str(t1w_path))

    # Convert to MGH format to access vox2ras method
    t1_mgh = nib.MGHImage(t1_nifti.dataobj, t1_nifti.affine)

    # convert to scanner RAS
    mri_landmarks = apply_trans(t1_mgh.header.get_vox2ras(), mri_landmarks)

    # convert to FreeSurfer T1 voxels (same scanner RAS as T1)
    mri_landmarks = apply_trans(fs_t1_mgh.header.get_ras2vox(), mri_landmarks)

    # now extract transformation matrix and put back to RAS coordinates of MRI
    vox2ras_tkr = fs_t1_mgh.header.get_vox2ras_tkr()
    mri_landmarks = apply_trans(vox2ras_tkr, mri_landmarks)
    mri_landmarks = mri_landmarks * 1e-3

    # Get MEG landmarks from the raw file
    _, ext = _parse_ext(meg_bids_path)
    if extra_params is None:
        extra_params = dict()
    if ext == '.fif':
        extra_params['allow_maxshield'] = True

    raw = read_raw_bids(bids_path=meg_bids_path, extra_params=extra_params)
    meg_coords_dict = _extract_landmarks(raw.info['dig'])
    meg_landmarks = np.asarray((meg_coords_dict['LPA'],
                                meg_coords_dict['NAS'],
                                meg_coords_dict['RPA']))

    # Given the two sets of points, fit the transform
    trans_fitted = fit_matched_points(src_pts=meg_landmarks,
                                      tgt_pts=mri_landmarks)
    trans = mne.transforms.Transform(fro='head', to='mri', trans=trans_fitted)
    return trans
