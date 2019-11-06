#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  hammer-vlsi plugin for Cadence Voltus.
#
#  See LICENSE for licence details.

import shutil
from typing import List, Dict, Optional, Callable, Tuple, Set, Any, cast
from itertools import product

import os
import errno
import json

from hammer_utils import get_or_else, optional_map, coerce_to_grid, check_on_grid, lcm_grid
from hammer_vlsi import HammerPowerTool, CadenceTool, HammerToolStep, \
from hammer_logging import HammerVLSILogging
import hammer_tech
from decimal import Decimal


class Voltus(HammerPowerTool, CadenceTool):

    @property
    def env_vars(self) -> Dict[str, str]:
        new_dict = dict(super().env_vars)
        new_dict["VOLTUS_BIN"] = self.get_setting("power.voltus.voltus_bin")
        return new_dict

    @property
    def steps(self) -> List[HammerToolStep]:
        return self.make_steps_from_methods([
            self.init_technology,
            self.init_design,
        ])

    def init_design(self) -> bool:
        # Read LEF layouts.
        lef_files = self.technology.read_libs([
            hammer_tech.filters.lef_filter
        ], hammer_tech.HammerTechnologyUtils.to_plain_item)
        verbose_append("read_lib -lef {{  {files}  }}".format(files=" ".join(lef_files)))

        #TODO(daniel): support hammer generated cpf
        power_spec = self.get_setting("power.inputs.power_spec")
        if not os.path.isfile(power_spec):
            raise ValueError("Power spec %s not found" % (power_spec)) # better error?

        verbose_append("read_power_domain -cpf {CPF}".format(CPF=power_spec))

        #TODO(daniel): add additional options
        verbose_append("read_spef {SPEF}".format(SPEF=self.spef_file))

        return True

tool = Voltus
