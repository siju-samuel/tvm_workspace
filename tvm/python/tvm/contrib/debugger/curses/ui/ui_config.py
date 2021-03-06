"""Configurations for TVM Debugger (tvmdbg) command-line interfaces."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import json
import os

from tvm.contrib.debugger.curses.ui import ui_common

RL = ui_common.RichLine


class CLIConfig(object):
    """Client-facing configurations for TVMDBG command-line interfaces."""

    _CONFIG_FILE_NAME = ".tvmdbg_config"

    _DEFAULT_CONFIG = [
        ("graph_recursion_depth", 20),
        ("mouse_mode", True),
    ]

    def __init__(self, config_file_path=None):
        self._config_file_path = (config_file_path or
                                  self._default_config_file_path())
        self._config = collections.OrderedDict(self._DEFAULT_CONFIG)
        if os.path.isfile(self._config_file_path):
            config = self._load_from_file()
            for key, value in config.items():
                self._config[key] = value
        self._save_to_file()

        self._set_callbacks = dict()

    def get(self, property_name):
        """Get the value of a property.

        Parameters
        ----------
        property_name : str
            Name of the property.

        Returns
        -------
        value : str
            Value of the property.
        """
        if property_name not in self._config:
            raise KeyError("%s is not a valid property name." % property_name)
        return self._config[property_name]

    def set(self, property_name, property_val):
        """Set the value of a property.

        Supports limitd property value types: `bool`, `int` and `str`.

        Parameters
        ----------
        property_name : str
           Name of the property.

        property_val : str
            Value of the property. If the property has `bool` type and this argument has `str` type,
            the `str` value will be parsed as a `bool`
        """
        if property_name not in self._config:
            raise KeyError("%s is not a valid property name." % property_name)

        orig_val = self._config[property_name]
        if isinstance(orig_val, bool):
            if isinstance(property_val, str):
                if property_val.lower() in ("1", "true", "t", "yes", "y", "on"):
                    property_val = True
                elif property_val.lower() in ("0", "false", "f", "no", "n", "off"):
                    property_val = False
                else:
                    raise ValueError(
                        "Invalid string value for bool type: %s" % property_val)
            else:
                property_val = bool(property_val)
        elif isinstance(orig_val, int):
            property_val = int(property_val)
        elif isinstance(orig_val, str):
            property_val = str(property_val)
        else:
            raise TypeError("Unsupported property type: %s" % type(orig_val))
        self._config[property_name] = property_val
        self._save_to_file()

        # Invoke set-callback.
        if property_name in self._set_callbacks:
            self._set_callbacks[property_name](self._config)

    def set_callback(self, property_name, callback):
        """Set a set-callback for given property.

        Parameters
        ----------
        property_name : str
            Name of the property.

        callback : callable
            The callback as a `callable` of signature: def cbk(config): where config is the config
            after it is set to the new value. The callback is invoked each time the set() method is
            called with the matching property_name.
        """
        if property_name not in self._config:
            raise KeyError("%s is not a valid property name." % property_name)
        if not callable(callback):
            raise TypeError("The callback object provided is not callable.")
        self._set_callbacks[property_name] = callback

    def _default_config_file_path(self):
        return os.path.join(os.path.expanduser("~"), self._CONFIG_FILE_NAME)

    def _save_to_file(self):
        try:
            with open(self._config_file_path, "w") as config_file:
                json.dump(self._config, config_file)
        except IOError:
            pass

    def summarize(self, highlight=None):
        """Get a text summary of the config.

        Parameters
        ----------
        highlight : str
            A property name to highlight in the output.

        Returns
        -------
        lines : object
            A `RichTextLines` output.
        """
        lines = [RL("Command-line configuration:", "bold"), RL("")]
        for name, val in self._config.items():
            highlight_attr = "bold" if name == highlight else None
            line = RL("  ")
            line += RL(name, ["underline", highlight_attr])
            line += RL(": ")
            line += RL(str(val), font_attr=highlight_attr)
            lines.append(line)
        return ui_common.rich_text_lines_frm_line_list(lines)

    def _load_from_file(self):
        try:
            with open(self._config_file_path, "r") as config_file:
                config_dict = json.load(config_file)
                config = collections.OrderedDict()
                for key in sorted(config_dict.keys()):
                    config[key] = config_dict[key]
                return config
        except (IOError, ValueError):
            # The reading of the config file may fail due to IO issues or file
            # corruption. We do not want tvmdbg to error out just because of that.
            return dict()
