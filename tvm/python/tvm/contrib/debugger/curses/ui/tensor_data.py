"""Format tensors (ndarrays) for screen display and navigation."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import re

import numpy as np
from tvm.contrib.debugger.curses.ui import ui_common
from tvm.contrib.debugger.curses.util import data_dump

_NUMPY_OMISSION = "...,"
_NUMPY_DEFAULT_EDGE_ITEMS = 3

_NUMBER_REGEX = re.compile(r"[-+]?([0-9][-+0-9eE\.]+|nan|inf)(\s|,|\])")

BEGIN_INDICES_KEY = "i0"
OMITTED_INDICES_KEY = "omitted"

DEFAULT_TENSOR_ELEMENT_HIGHLIGHT_FONT_ATTR = "bold"


class HighlightOptions(object):
    """Options for highlighting elements of a tensor."""

    def __init__(self,
                 criterion,
                 description=None,
                 font_attr=DEFAULT_TENSOR_ELEMENT_HIGHLIGHT_FONT_ATTR):
        """Constructor of HighlightOptions.

        Parameters
        ----------
        criterion : (callable)
            A callable of the following signature: def to_highlight(X):
            This callable will be used as the argument of np.argwhere() to
            determine which elements of the tensor are to be highlighted.

        description : str
            Description of the highlight criterion embodied by criterion.

        font_attr : str
            Font attribute to be applied to the highlighted elements.

        """

        self.criterion = criterion
        self.description = description
        self.font_attr = font_attr


def format_tensor(tensor,
                  tensor_label,
                  include_metadata=False,
                  auxiliary_message=None,
                  include_numeric_summary=False,
                  np_printoptions=None,
                  highlight_options=None):
    """Generate a RichTextLines object showing a tensor in formatted style.

    Parameters
    ----------
    tensor : tensor
        The tensor to be displayed, as a numpy ndarray or other
        appropriate format (e.g., None representing uninitialized tensors).

    tensor_label : str
        A label for the tensor, as a string. If set to None, will
        suppress the tensor name line in the return value.

    include_metadata : metadata
        Whether metadata such as dtype and shape are to be included in the formatted text.

    auxiliary_message : str
        An auxiliary message to display under the tensor label,  dtype and shape information lines.

    include_numeric_summary : bool
        Whether a text summary of the numeric values (if applicable) will be included.

    np_printoptions : str
        A dictionary of keyword arguments that are passed to a call of np.set_printoptions() to
        set the text format for display numpy ndarrays.

    highlight_options : HighlightOptions
        options for highlighting elements of the tensor.

    Returns
    -------
    lines : object
        A RichTextLines object. Its annotation field has line-by-line markups to
        indicate which indices in the array the first element of each line corresponds to.
    """
    lines = []
    font_attr_segs = {}

    if tensor_label is not None:
        lines.append("Tensor \"%s\":" % tensor_label)
        suffix = tensor_label.split(":")[-1]
        if suffix.isdigit():
            # Suffix is a number. Assume it is the output slot index.
            font_attr_segs[0] = [(8, 8 + len(tensor_label), "white")]
        else:
            # Suffix is not a number. It is auxiliary information such as the debug
            # op type. In this case, highlight the suffix with a different color.
            debug_op_len = len(suffix)
            proper_len = len(tensor_label) - debug_op_len - 1
            font_attr_segs[0] = [
                (8, 8 + proper_len, "white"),
                (8 + proper_len + 1, 8 + proper_len + 1 + debug_op_len, "yellow")
            ]

    if isinstance(tensor, data_dump.InconvertibleTensorProto):
        if lines:
            lines.append("")
        lines.extend(str(tensor).split("\n"))
        return ui_common.RichTextLines(lines)
    elif not isinstance(tensor, np.ndarray):
        # If tensor is not a np.ndarray, return simple text-line representation of
        # the object without annotations.
        if lines:
            lines.append("")
        lines.extend(repr(tensor).split("\n"))
        return ui_common.RichTextLines(lines)

    if include_metadata:
        lines.append("  dtype: %s" % str(tensor.dtype))
        lines.append("  shape: %s" % str(tensor.shape).replace("L", ""))

    if lines:
        lines.append("")
    formatted = ui_common.RichTextLines(
        lines, font_attr_segs=font_attr_segs)

    if auxiliary_message:
        formatted.extend(auxiliary_message)

    if include_numeric_summary:
        formatted.append("Numeric summary:")
        formatted.extend(numeric_summary(tensor))
        formatted.append("")

    # Apply custom string formatting options for numpy ndarray.
    if np_printoptions is not None:
        np.set_printoptions(**np_printoptions)

    array_lines = repr(tensor).split("\n")
    if tensor.dtype.type is not np.string_:
        # Parse array lines to get beginning indices for each line.

        annotations = _annotate_ndarray_lines(
            array_lines, tensor, np_printoptions=np_printoptions)
    else:
        annotations = None
    formatted_array = ui_common.RichTextLines(
        array_lines, annotations=annotations)
    formatted.extend(formatted_array)

    # Perform optional highlighting.
    if highlight_options is not None:
        indices_list = list(np.argwhere(highlight_options.criterion(tensor)))

        total_elements = np.size(tensor)
        highlight_summary = "Highlighted%s: %d of %d element(s) (%.2f%%)" % (
            "(%s)" % highlight_options.description if highlight_options.description
            else "", len(indices_list), total_elements,
            len(indices_list) / float(total_elements) * 100.0)

        formatted.lines[0] += " " + highlight_summary

        if indices_list:
            indices_list = [list(indices) for indices in indices_list]

            are_omitted, rows, start_cols, end_cols = locate_tensor_element(
                formatted, indices_list)
            for is_omitted, row, start_col, end_col in zip(are_omitted, rows,
                                                           start_cols, end_cols):
                if is_omitted or start_col is None or end_col is None:
                    continue

                if row in formatted.font_attr_segs:
                    formatted.font_attr_segs[row].append(
                        (start_col, end_col, highlight_options.font_attr))
                else:
                    formatted.font_attr_segs[row] = [(start_col, end_col,
                                                      highlight_options.font_attr)]

    return formatted


def _annotate_ndarray_lines(array_lines,
                            tensor,
                            np_printoptions=None,
                            offset=0):
    """Generate annotations for line-by-line begin indices of tensor text.

    Parse the numpy-generated text representation of a numpy ndarray to
    determine the indices of the first element of each text line (if any
    element is present in the line).

    Parameters
    ----------
    array_lines : list of str
        Text lines representing the tensor, as a list of str.

    tensor : str
        The tensor being formatted as string.

    np_printoptions : str
        A dictionary of keyword arguments that are passed to a call of np.set_printoptions().

    offset : int
        Line number offset applied to the line indices in the returned annotation.

    Returns
    -------
    annotations : dict
        An annotation as a dict.
    """

    if np_printoptions and "edgeitems" in np_printoptions:
        edge_items = np_printoptions["edgeitems"]
    else:
        edge_items = _NUMPY_DEFAULT_EDGE_ITEMS

    annotations = {}

    # Put metadata about the tensor in the annotations["tensor_metadata"].
    annotations["tensor_metadata"] = {
        "dtype": tensor.dtype, "shape": tensor.shape}

    dims = np.shape(tensor)
    ndims = len(dims)
    if ndims == 0:
        # No indices for a 0D tensor.
        return annotations

    curr_indices = [0] * len(dims)
    curr_dim = 0
    len_array_lines = len(array_lines)
    for i in range(len_array_lines):
        line = array_lines[i].strip()

        if not line:
            # Skip empty lines, which can appear for >= 3D arrays.
            continue

        if line == _NUMPY_OMISSION:
            annotations[offset + i] = {OMITTED_INDICES_KEY: copy.copy(curr_indices)}
            curr_indices[curr_dim - 1] = dims[curr_dim - 1] - edge_items
        else:
            num_lbrackets = line.count("[")
            num_rbrackets = line.count("]")

            curr_dim += num_lbrackets - num_rbrackets

            annotations[offset + i] = {BEGIN_INDICES_KEY: copy.copy(curr_indices)}
            if num_rbrackets == 0:
                line_content = line[line.rfind("[") + 1:]
                num_elements = line_content.count(",")
                curr_indices[curr_dim - 1] += num_elements
            else:
                if curr_dim > 0:
                    curr_indices[curr_dim - 1] += 1
                    for k in range(curr_dim, ndims):
                        curr_indices[k] = 0

    return annotations


def locate_tensor_element(formatted, indices):
    """Locate a tensor element in formatted text lines, given element indices.

    Given a RichTextLines object representing a tensor and indices of the sought
    element, return the row number at which the element is located (if exists).

    Parameters
    ----------
    formatted : RichTextLines object
        A RichTextLines object containing formatted text lines representing the tensor.

    indices : list
        Indices of the sought element, as a list of int or a list of list of int. The former case
        is for a single set of indices to look up, whereas the latter case is for looking up a batch
        of indices sets at once. In the latter case, the indices must be in ascending order, or a
        ValueError will be raised.

    Returns
    -------
    are_omitted : bool
        A boolean indicating whether the element falls into an omitted line.

    row_indices : int
        Row index.

    start_columns : int
        Column start index, i.e., first column in which the representationof the specified tensor
        starts, if it can be determined. If it cannot be determined (e.g., due to ellipsis), None.

    end_columns : int
        Column end index, i.e., the column right after the last column that
        represents the specified tensor. Iff it cannot be determined, None.
    """

    if isinstance(indices[0], list):
        indices_list = indices
        input_batch = True
    else:
        indices_list = [indices]
        input_batch = False

    # Check that tensor_metadata is available.
    if "tensor_metadata" not in formatted.annotations:
        raise AttributeError("tensor_metadata is not available in annotations.")

    # Sanity check on input argument.
    _validate_indices_list(indices_list, formatted)

    dims = formatted.annotations["tensor_metadata"]["shape"]
    batch_size = len(indices_list)
    lines = formatted.lines
    annot = formatted.annotations
    prev_r = 0
    prev_line = ""
    prev_indices = [0] * len(dims)

    # Initialize return values
    are_omitted = [None] * batch_size
    row_indices = [None] * batch_size
    start_columns = [None] * batch_size
    end_columns = [None] * batch_size

    batch_pos = 0  # Current position in the batch.

    len_lines = len(lines)
    for i in range(len_lines):
        if i not in annot:
            continue

        if BEGIN_INDICES_KEY in annot[i]:
            indices_key = BEGIN_INDICES_KEY
        elif OMITTED_INDICES_KEY in annot[i]:
            indices_key = OMITTED_INDICES_KEY

        matching_indices_list = [
            ind for ind in indices_list[batch_pos:]
            if prev_indices <= ind < annot[i][indices_key]
        ]

        if matching_indices_list:
            num_matches = len(matching_indices_list)

            match_start_columns, match_end_columns = _locate_elements_in_line(
                prev_line, matching_indices_list, prev_indices)

            start_columns[batch_pos:batch_pos + num_matches] = match_start_columns
            end_columns[batch_pos:batch_pos + num_matches] = match_end_columns
            are_omitted[batch_pos:batch_pos + num_matches] = [
                OMITTED_INDICES_KEY in annot[prev_r]] * num_matches
            row_indices[batch_pos:batch_pos + num_matches] = [prev_r] * num_matches

            batch_pos += num_matches
            if batch_pos >= batch_size:
                break

        prev_r = i
        prev_line = lines[i]
        prev_indices = annot[i][indices_key]

    if batch_pos < batch_size:
        matching_indices_list = indices_list[batch_pos:]
        num_matches = len(matching_indices_list)

        match_start_columns, match_end_columns = _locate_elements_in_line(
            prev_line, matching_indices_list, prev_indices)

        start_columns[batch_pos:batch_pos + num_matches] = match_start_columns
        end_columns[batch_pos:batch_pos + num_matches] = match_end_columns
        are_omitted[batch_pos:batch_pos + num_matches] = [
            OMITTED_INDICES_KEY in annot[prev_r]] * num_matches
        row_indices[batch_pos:batch_pos + num_matches] = [prev_r] * num_matches

    if input_batch:
        return are_omitted, row_indices, start_columns, end_columns
    return are_omitted[0], row_indices[0], start_columns[0], end_columns[0]


def _validate_indices_list(indices_list, formatted):
    prev_ind = None
    for ind in indices_list:
        # Check indices match tensor dimensions.
        dims = formatted.annotations["tensor_metadata"]["shape"]
        if len(ind) != len(dims):
            raise ValueError("Dimensions mismatch: requested: %d; actual: %d" %
                             (len(ind), len(dims)))

        # Check indices is within size limits.
        for req_idx, siz in zip(ind, dims):
            if req_idx >= siz:
                raise ValueError("Indices exceed tensor dimensions.")
            if req_idx < 0:
                raise ValueError("Indices contain negative value(s).")

        # Check indices are in ascending order.
        if prev_ind and ind < prev_ind:
            raise ValueError("Input indices sets are not in ascending order.")

        prev_ind = ind


def _locate_elements_in_line(line, indices_list, ref_indices):
    """Determine the start and end indices of an element in a line.

    Parameters
    ----------
    line : str
        the line in which the element is to be sought.

    indices_list : list of list of int
        list of indices of the element to search for. Assumes that indices in the batch are unique
        and sorted in ascending order.

    ref_indices : list of int
        reference indices, i.e., the indices of the first element represented in the line.

    Returns
    -------
    start_columns: list of int
      start column indices, if found. If not found, None.

    end_columns: list of int
      end column indices, if found. If not found, None. If found, the element is represented in the
      left-closed-right-open interval [start_column, end_column].
    """

    batch_size = len(indices_list)
    offsets = [indices[-1] - ref_indices[-1] for indices in indices_list]

    start_columns = [None] * batch_size
    end_columns = [None] * batch_size

    if _NUMPY_OMISSION in line:
        ellipsis_index = line.find(_NUMPY_OMISSION)
    else:
        ellipsis_index = len(line)

    matches_iter = re.finditer(_NUMBER_REGEX, line)

    batch_pos = 0

    offset_counter = 0
    for match in matches_iter:
        if match.start() > ellipsis_index:
            # Do not attempt to search beyond ellipsis.
            break

        if offset_counter == offsets[batch_pos]:
            start_columns[batch_pos] = match.start()
            # Remove the final comma, right bracket, or whitespace.
            end_columns[batch_pos] = match.end() - 1

            batch_pos += 1
            if batch_pos >= batch_size:
                break

        offset_counter += 1

    return start_columns, end_columns


def _pad_string_to_length(string, length):
    return " " * (length - len(string)) + string


def numeric_summary(tensor):
    """Get a text summary of a numeric tensor.

    This summary is only available for numeric (int*, float*, complex*) and
    Boolean tensors.

    Parameters
    ----------
    tensor : numpy.ndarray
        the tensor value object to be summarized.

    Returns
    -------
    lines : object
        The summary text as a `RichTextLines` object. If the type of `tensor` is not
        numeric or Boolean, a single-line `RichTextLines` object containing a
        warning message will reflect that.
    """

    def _counts_summary(counts, skip_zeros=True, total_count=None):
        """Format values as a two-row table."""
        if skip_zeros:
            counts = [(count_key, count_val) for count_key, count_val in counts
                      if count_val]
        max_common_len = 0
        for count_key, count_val in counts:
            count_val_str = str(count_val)
            common_len = max(len(count_key) + 1, len(count_val_str) + 1)
            max_common_len = max(common_len, max_common_len)

        key_line = ui_common.RichLine("|")
        val_line = ui_common.RichLine("|")
        for count_key, count_val in counts:
            count_val_str = str(count_val)
            key_line += _pad_string_to_length(count_key, max_common_len)
            val_line += _pad_string_to_length(count_val_str, max_common_len)
        key_line += " |"
        val_line += " |"

        if total_count is not None:
            total_key_str = "total"
            total_val_str = str(total_count)
            max_common_len = max(len(total_key_str) + 1, len(total_val_str))
            total_key_str = _pad_string_to_length(total_key_str, max_common_len)
            total_val_str = _pad_string_to_length(total_val_str, max_common_len)
            key_line += total_key_str + " |"
            val_line += total_val_str + " |"

        return ui_common.rich_text_lines_frm_line_list(
            [key_line, val_line])

    if not isinstance(tensor, np.ndarray) or not np.size(tensor):
        return ui_common.RichTextLines([
            "No numeric summary available due to empty tensor."])
    elif (np.issubdtype(tensor.dtype, np.floating) or
          np.issubdtype(tensor.dtype, np.complex) or
          np.issubdtype(tensor.dtype, np.integer)):
        counts = [
            ("nan", np.sum(np.isnan(tensor))),
            ("-inf", np.sum(np.isneginf(tensor))),
            ("-", np.sum(np.logical_and(
                tensor < 0.0, np.logical_not(np.isneginf(tensor))))),
            ("0", np.sum(tensor == 0.0)),
            ("+", np.sum(np.logical_and(
                tensor > 0.0, np.logical_not(np.isposinf(tensor))))),
            ("+inf", np.sum(np.isposinf(tensor)))]
        output = _counts_summary(counts, total_count=np.size(tensor))

        valid_array = tensor[
            np.logical_not(np.logical_or(np.isinf(tensor), np.isnan(tensor)))]
        if np.size(valid_array):
            stats = [
                ("min", np.min(valid_array)),
                ("max", np.max(valid_array)),
                ("mean", np.mean(valid_array)),
                ("std", np.std(valid_array))]
            output.extend(_counts_summary(stats, skip_zeros=False))
        return output
    elif tensor.dtype == np.bool:
        counts = [
            ("False", np.sum(tensor == 0)),
            ("True", np.sum(tensor > 0)), ]
        return _counts_summary(counts, total_count=np.size(tensor))
    return ui_common.RichTextLines([
        "No numeric summary available due to tensor dtype: %s." % tensor.dtype])