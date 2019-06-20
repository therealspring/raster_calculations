"""Process a raster calculator plain text expression."""
import pickle
import time
import sys
import os
import logging
import urllib.request

from retrying import retry
from osgeo import osr
from osgeo import gdal
import pygeoprocessing
import numpy

LOGGER = logging.getLogger(__name__)


def evaluate_calculation(args, task_graph, workspace_dir):
    """Evaluate raster calculator expression object.

    Parameters:
        args['expression'] (str): a symbolic arithmetic expression
            representing the desired calculation.
        args['symbol_to_path_map'] (dict): dictionary mapping symbols in
            `expression` to either arbitrary functions, raster paths, or URLs.
            In the case of the latter, the file will be downloaded to a
            `workspace_dir`
        args['target_nodata'] (numeric): desired output nodata value.
        args['target_raster_path'] (str): path to output raster.
        workspace_dir (str): path to a directory that can be used to store
            intermediate values.

    Returns:
        None.
    """
    args_copy = args.copy()
    expression_id = os.path.splitext(
        os.path.basename(args_copy['target_raster_path']))[0]
    expression_workspace_path = os.path.join(workspace_dir, expression_id)
    expression_ecoshard_path = os.path.join(
        expression_workspace_path, 'ecoshard')
    try:
        os.makedirs(expression_ecoshard_path)
    except OSError:
        pass
    # process ecoshards if necessary
    symbol_to_path_band_map = {}
    download_task_list = []
    for symbol, path in args_copy['symbol_to_path_map'].items():
        if isinstance(path, str) and (
                path.startswith('http://') or path.startswith('https://')):
            # download to local file
            local_path = os.path.join(
                expression_ecoshard_path,
                os.path.basename(path))
            download_task = task_graph.add_task(
                func=download_url,
                args=(path, local_path),
                target_path_list=[local_path],
                task_name='download %s' % local_path)
            download_task_list.append(download_task)
            symbol_to_path_band_map[symbol] = (local_path, 1)
        else:
            symbol_to_path_band_map[symbol] = (path, 1)

    # should i process rasters here?
    try:
        process_raster_churn_dir = os.path.join(
            workspace_dir, 'processed_rasters_dir')
        os.makedirs(process_raster_churn_dir)
    except OSError:
        pass
    processed_raster_list_file_path = os.path.join(
        process_raster_churn_dir, 'processed_raster_list.pickle')
    download_task.join()
    LOGGER.debug(symbol_to_path_band_map)
    _preprocess_rasters(
        [path for path in symbol_to_path_band_map.values()],
        process_raster_churn_dir, processed_raster_list_file_path)

    with open(processed_raster_list_file_path) as processed_raster_list_file:
        processed_raster_path_list = pickle.load(processed_raster_list_file)

    for symbol, raster_path in zip(
            args_copy['symbol_to_path_band_map'],
            processed_raster_path_list):
        path_band_id = args_copy['symbol_to_path_band_map'][symbol][1]
        args_copy['symbol_to_path_band_map'][symbol] = (
            raster_path, path_band_id)

    # this sets a common target sr, pixel size, and resample method .
    args_copy.update({
        'churn_dir': workspace_dir,
        'symbol_to_path_band_map': symbol_to_path_band_map,
        })
    del args_copy['symbol_to_path_map']
    build_overview = (
        'build_overview' in args_copy and args_copy['build_overview'])
    if 'build_overview' in args_copy:
        del args_copy['build_overview']

    if not args['expression'].startswith('mask(raster'):
        eval_raster_task = task_graph.add_task(
            func=pygeoprocessing.evaluate_raster_calculator_expression,
            kwargs=args_copy,
            dependent_task_list=download_task_list,
            target_path_list=[args_copy['target_raster_path']],
            task_name='%s -> %s' % (
                args_copy['expression'],
                os.path.basename(args_copy['target_raster_path'])))
    else:
        # parse out array
        arg_list = args['expression'].split(',')
        # the first 1 to n-1 args must be integers
        mask_val_list = [int(val) for val in arg_list[1:-1]]
        # the last argument could be 'invert=?'
        if 'invert' in arg_list[-1]:
            invert = 'True' in arg_list[-1]
        else:
            # if it's not, it'll be another integer
            mask_val_list.append(int(arg_list[-1][:-1]))
            invert = False
        eval_raster_task = task_graph.add_task(
            func=mask_raster_by_array,
            args=(
                symbol_to_path_band_map['raster'],
                numpy.array(mask_val_list),
                args_copy['target_raster_path'], invert),
            target_path_list=[args_copy['target_raster_path']],
            dependent_task_list=download_task_list,
            task_name='mask raster %s by %s -> %s' % (
                symbol_to_path_band_map['raster'],
                str(mask_val_list), args_copy['target_raster_path']))
    if build_overview:
        overview_path = '%s.ovr' % (
            args_copy['target_raster_path'])
        task_graph.add_task(
            func=build_overviews,
            args=(args_copy['target_raster_path'],),
            dependent_task_list=[eval_raster_task],
            target_path_list=[overview_path],
            task_name='overview for %s' % args_copy['target_raster_path'])


def mask_raster_by_array(
        raster_path_band, mask_array, target_raster_path, invert=False):
    """Mask the given raster path/band by a set of integers.

    Parameters:
        raster_path_band (tuple): a raster path/band indicating the band to
            apply the mask operation.
        mask_array (list/numpy.ndarray): a sequence of integers that will be
            used to set a mask.
        target_raster_path (str): path to output raster which will have 1s
            where the original raster band had a value found in `mask_array`,
            0 if not found, and nodata if originally nodata.
        invert (bool): if true makes a mask of all values in raster band that
            are *not* in `mask_array`.

    Returns:
        None.

    """
    raster_info = pygeoprocessing.get_raster_info(raster_path_band[0])
    pygeoprocessing.raster_calculator(
        [raster_path_band,
         (raster_info['nodata'][raster_path_band[1]-1], 'raw'),
         (numpy.array(mask_array), 'raw'), (2, 'raw'), (invert, 'raw')],
        _mask_raster_op, target_raster_path, gdal.GDT_Byte, 2)


def _mask_raster_op(array, array_nodata, mask_values, target_nodata, invert):
    """Mask array by *mask_values list."""
    result = numpy.empty(array.shape, dtype=numpy.int8)
    result[:] = target_nodata
    valid_mask = array != array_nodata
    result[valid_mask] = numpy.in1d(
        array[valid_mask], mask_values, invert=invert)
    return result


def build_overviews(raster_path):
    """Build external overviews for raster."""
    raster = gdal.Open(raster_path, gdal.OF_RASTER)
    min_dimension = min(raster.RasterXSize, raster.RasterYSize)
    overview_levels = []
    current_level = 2
    while True:
        if min_dimension // current_level == 0:
            break
        overview_levels.append(current_level)
        current_level *= 2

    gdal.SetConfigOption('COMPRESS_OVERVIEW', 'LZW')
    raster.BuildOverviews(
        'average', overview_levels, callback=_make_logger_callback(
            'build overview for ' + os.path.basename(raster_path) +
            '%.2f%% complete'))




def _make_logger_callback(message):
    """Build a timed logger callback that prints ``message`` replaced.

    Parameters:
        message (string): a string that expects 2 placement %% variables,
            first for % complete from ``df_complete``, second from
            ``p_progress_arg[0]``.

    Returns:
        Function with signature:
            logger_callback(df_complete, psz_message, p_progress_arg)

    """
    def logger_callback(df_complete, _, p_progress_arg):
        """Argument names come from the GDAL API for callbacks."""
        try:
            current_time = time.time()
            if ((current_time - logger_callback.last_time) > 5.0 or
                    (df_complete == 1.0 and
                     logger_callback.total_time >= 5.0)):
                # In some multiprocess applications I was encountering a
                # ``p_progress_arg`` of None. This is unexpected and I suspect
                # was an issue for some kind of GDAL race condition. So I'm
                # guarding against it here and reporting an appropriate log
                # if it occurs.
                if p_progress_arg:
                    LOGGER.info(message, df_complete * 100, p_progress_arg[0])
                else:
                    LOGGER.info(
                        'p_progress_arg is None df_complete: %s, message: %s',
                        df_complete, message)
                logger_callback.last_time = current_time
                logger_callback.total_time += current_time
        except AttributeError:
            logger_callback.last_time = time.time()
            logger_callback.total_time = 0.0

    return logger_callback


def _preprocess_rasters(
        base_raster_path_list, churn_dir, target_sr_wkt=None,
        target_pixel_size=None, resample_method='near'):
    """Process base raster path list so it can be used in raster calcs.

    Parameters:
        base_raster_path_list (list): list of arbitrary rasters.
        churn_dir (str): path to a directory that can be used to write
            temporary files that could be used later for
            caching/reproducibility.
        target_sr_wkt (string): if not None, this is the desired
            projection of the target rasters in Well Known Text format. If
            None and all symbol rasters have the same projection, that
            projection will be used. Otherwise a ValueError is raised
            indicating that the rasters are in different projections with
            no guidance to resolve.
        target_pixel_size (tuple): It not None, desired output target pixel
            size. A ValueError is raised if symbol rasters are different
            pixel sizes and this value is None.
        resample_method (str): if the symbol rasters need to be resized for
            any reason, this method is used. The value can be one of:
            "near|bilinear|cubic|cubicspline|lanczos|average|mode|max".

    Returns:
        list of raster paths that can be used in raster calcs, note this may
        be the original list of rasters or they may have been created by
        this call.

    """
    resample_inputs = False

    base_info_list = [
        pygeoprocessing.get_raster_info(path)
        for path in base_raster_path_list]
    base_projection_list = [info['projection'] for info in base_info_list]
    base_pixel_list = [info['pixel_size'] for info in base_info_list]
    base_raster_shape_list = [info['raster_size'] for info in base_info_list]

    target_sr_wkt = None
    if len(set(base_projection_list)) != 1:
        if target_sr_wkt is not None:
            raise ValueError(
                "Projections of base rasters are not equal and there "
                "is no `target_sr_wkt` defined.\nprojection list: %s",
                str(base_projection_list))
        else:
            LOGGER.info('projections are different')
            target_srs = osr.SpatialReference()
            target_srs.ImportFromWkt(target_sr_wkt)
            target_sr_wkt = target_srs.ExportToWkt()
            resample_inputs = True

    if len(set(base_pixel_list)) != 1:
        if target_pixel_size is None:
            raise ValueError(
                "base and reference pixel sizes are different and no target "
                "is defined.\nbase pixel sizes: %s", str(base_pixel_list))
        LOGGER.info('pixel sizes are different')
        resample_inputs = True
    else:
        # else use the pixel size they all have
        target_pixel_size = base_pixel_list[0]

    if len(set(base_raster_shape_list)) != 1:
        LOGGER.info('raster shapes different')
        resample_inputs = True

    if resample_inputs:
        LOGGER.info("need to align/reproject inputs to apply calculation")
        try:
            os.makedirs(churn_dir)
        except OSError:
            LOGGER.debug('churn dir %s already exists', churn_dir)

        operand_raster_path_list = [
            os.path.join(churn_dir, os.path.basename(path)) for path in
            base_raster_path_list]
        pygeoprocessing.align_and_resize_raster_stack(
            base_raster_path_list, operand_raster_path_list,
            [resample_method]*len(base_raster_path_list),
            target_pixel_size, 'intersection', target_sr_wkt=target_sr_wkt)
        return operand_raster_path_list
    else:
        return base_raster_path_list


@retry(wait_exponential_multiplier=1000, wait_exponential_max=10000)
def download_url(url, target_path, skip_if_target_exists=False):
    """Download `url` to `target_path`."""
    try:
        if skip_if_target_exists and os.path.exists(target_path):
            return
        with open(target_path, 'wb') as target_file:
            with urllib.request.urlopen(url) as url_stream:
                meta = url_stream.info()
                file_size = int(meta["Content-Length"])
                LOGGER.info(
                    "Downloading: %s Bytes: %s" % (target_path, file_size))

                downloaded_so_far = 0
                block_size = 2**20
                while True:
                    data_buffer = url_stream.read(block_size)
                    if not data_buffer:
                        break
                    downloaded_so_far += len(data_buffer)
                    target_file.write(data_buffer)
                    status = r"%10d  [%3.2f%%]" % (
                        downloaded_so_far, downloaded_so_far * 100. /
                        file_size)
                    LOGGER.info(status)
    except:
        LOGGER.exception("Exception encountered, trying again.")
        raise
