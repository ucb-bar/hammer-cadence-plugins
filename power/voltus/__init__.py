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
from hammer_vlsi import HammerPowerTool, CadenceTool, HammerToolStep, MMMCCornerType
from hammer_logging import HammerVLSILogging
import hammer_tech


class Voltus(HammerPowerTool, CadenceTool):
    @property
    def post_synth_sdc(self) -> Optional[str]:
        # No post-synth SDC input for power...
        return None

    def tool_config_prefix(self) -> str:
        return "power.voltus"

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
            self.static_power,
            self.static_rail,
            self.active_power,
            self.active_rail,
            self.run_voltus
        ])

    # TODO (daniel) library characterization
    def init_technology(self) -> bool:
        return True

    def init_design(self) -> bool:
        verbose_append = self.verbose_append

        # Read LEF layouts.
        #lef_files = self.technology.read_libs([
        #    hammer_tech.filters.lef_filter
        #], hammer_tech.HammerTechnologyUtils.to_plain_item)
        #verbose_append("read_lib -lef {{  {files}  }}".format(files=" ".join(lef_files)))

        ##TODO(daniel): support hammer generated cpf
        #power_spec = self.get_setting("power.inputs.power_spec")
        #if not os.path.isfile(power_spec):
        #    raise ValueError("Power spec %s not found" % (power_spec)) # better error?

        #verbose_append("read_power_domain -cpf {CPF}".format(CPF=power_spec))

        ##TODO(daniel): add additional options
        #verbose_append("read_spef {SPEF}".format(SPEF=self.spef_file))
        verbose_append("set_multi_cpu_usage -local_cpu {}".format(self.get_setting("vlsi.core.max_threads")))

        innovus_db = self.get_setting("power.inputs.database")
        if innovus_db is None or not os.path.isdir(innovus_db):
            raise ValueError("Innovus database %s not found" % (innovus_db))

        verbose_append("read_db {}".format(innovus_db))

        # TODO (daniel) deal with multiple power domains
        for power_net in self.get_all_power_nets():
            vdd_net = power_net.name
        for gnd_net in self.get_all_ground_nets():
            vss_net = gnd_net.name
        verbose_append("set_power_pads -net {VDD} -format defpin".format(VDD=vdd_net))
        verbose_append("set_power_pads -net {VSS} -format defpin".format(VSS=vss_net))

        # TODO (daniel) deal with multiplce corners
        corners = self.get_mmmc_corners()
        for corner in corners:
            if corner.type is MMMCCornerType.Setup:
                setup_view_name = "{cname}.setup_view".format(cname=corner.name)
            elif corner.type is MMMCCornerType.Hold:
                hold_view_name = "{cname}.hold_view".format(cname=corner.name)
        verbose_append("set_analysis_view -setup {SETUP_VIEW} -hold {HOLD_VIEW}".format(SETUP_VIEW=setup_view_name, HOLD_VIEW=hold_view_name))

        return True

    def static_power(self) -> bool:
        verbose_append = self.verbose_append

        verbose_append("set_db power_method static")
        verbose_append("set_db power_write_static_currents true")
        verbose_append("set_db power_write_db true")
        verbose_append("report_power -out_dir staticPowerReports")

        return True

    def static_rail(self) -> bool:
        verbose_append = self.verbose_append

        # TODO (daniel) change to struct
        if self.get_setting("power.inputs.static_rail"):
            # TODO (daniel) add more setting parameters
            verbose_append("set_rail_anaylsis_config -analysis_view VIEW -method era_static -accuracy xd -extraction_techfile QRC_TECHFILE")
            verbose_append("set_power_data -format current {FILE NAMES}")
            verbose_append("report_rail -output_dir staticRailReports -type domain AO")

        return True

    def active_power(self) -> bool:
        verbose_append = self.verbose_append

        #active_power_mode = self.get_setting("power.inputs.active_power_mode")

        # Active Vectorless Power Analysis
        verbose_append("set_db power_method dynamic_vectorless")
        # TODO (daniel) add the resolution as an option?
        verbose_append("set_dynamic_power_simulation -resolution 500ps")
        verbose_append("report_power -out_dir activePowerReports")

        # TODO (daniel) deal with different tb/dut hierarchies
        tb_name = self.get_setting("power.inputs.tb_name")
        tb_dut = self.get_setting("power.inputs.tb_dut")
        tb_scope = "{}/{}".format(tb_name, tb_dut)

        # Active Vectorbased Power Analysis
        verbose_append("set_db power_method dynamic_vectorbased")
        for vcd_path in self.get_setting("power.inputs.waveforms"):
            verbose_append("read_activity_file -reset -format VCD {VCD_PATH} -start 0 -end 10000 -scope {TESTBENCH}".format(VCD_PATH=vcd_path, TESTBENCH=tb_scope))
            # TODO (daniel) make this change name based on input vector file
            verbose_append("report_vector_profile -detailed_report true -out_file activePowerProfile.{VCD_FILE}".format(VCD_FILE=vcd_path.split('/')[-1]))
            verbose_append("report_power -out_dir activePower.{VCD_FILE}".format(VCD_FILE=vcd_path.split('/')[-1]))

        return True

    def active_rail(self) -> bool:
        return True

    def run_voltus(self) -> bool:
        verbose_append = self.verbose_append

        """Close out the power script and run Voltus"""
        # Quit Voltus
        verbose_append("quit")

        # Create power analysis script
        power_tcl_filename = os.path.join(self.run_dir, "power.tcl")

        with open(power_tcl_filename, "w") as f:
            f.write("\n".join(self.output))

        # Build args
        args = [
            self.get_setting("power.voltus.voltus_bin"),
            "-init", power_tcl_filename,
            "-no_gui",
            "-common_ui"
        ]

        HammerVLSILogging.enable_colour = False
        HammerVLSILogging.enable_tag = False

        self.run_executable(args, cwd=self.run_dir)

        HammerVLSILogging.enable_colour = True
        HammerVLSILogging.enable_tag = True

        return True



tool = Voltus
