#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  hammer-vlsi plugin for Cadence Joules.
#
#  See LICENSE for licence details.

import shutil
from typing import List, Dict, Optional, Callable, Tuple, Set, Any, cast
from itertools import product

import os
import errno
import json

from hammer_utils import get_or_else, optional_map, coerce_to_grid, check_on_grid, lcm_grid
from hammer_vlsi import HammerPowerTool, HammerToolStep, MMMCCornerType, TimeValue
from hammer_logging import HammerVLSILogging
import hammer_tech

import sys
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)),"../../common"))
from tool import CadenceTool


class Joules(HammerPowerTool, CadenceTool):
    @property
    def post_synth_sdc(self) -> Optional[str]:
        # No post-synth SDC input for power...
        return None

    def tool_config_prefix(self) -> str:
        return "power.joules"

    @property
    def env_vars(self) -> Dict[str, str]:
        new_dict = dict(super().env_vars)
        new_dict["JOULES_BIN"] = self.get_setting("power.joules.joules_bin")
        return new_dict

    @property
    def steps(self) -> List[HammerToolStep]:
        return self.make_steps_from_methods([
            self.init_technology,
            self.init_design,
            self.run_joules
        ])

    def init_technology(self) -> bool:
        # libs, define RAMs, define corners
        verbose_append = self.verbose_append

        # TODO integrate TT corner power
        corners = self.get_mmmc_corners()
        setup = corners[0]
        hold = corners[0]

        for corner in corners:
            if corner.type is MMMCCornerType.Setup:
               setup = corner
            if corner.type is MMMCCornerType.Hold:
                hold = corner

        setup_lib = self.get_timing_libs(setup)
        hold_lib = self.get_timing_libs(hold)

        verbose_append("read_libs {HOLD_LIB}".format(HOLD_LIB=hold_lib))

        return True

    def init_design(self) -> bool:
        verbose_append = self.verbose_append

        # verbose_append("set_multi_cpu_usage -local_cpu {}".format(self.get_setting("vlsi.core.max_threads")))

        # TODO change to self.hdl
        hdl = self.get_setting("power.inputs.hdl")
        top_module = self.get_setting("power.inputs.top_module")
        #tb_name = self.get_setting("power.inputs.tb_name")
        tb_name = self.tb_name
        #tb_dut = self.get_setting("power.inputs.tb_dut")
        tb_dut = self.tb_dut

        # Read in the design files
        verbose_append("read_hdl {HDL}".format(HDL=" ".join(hdl)))

        # Setup the power specification
        power_spec_arg = self.map_power_spec_name()
        power_spec_file = self.create_power_spec()

        verbose_append("read_power_intent -{tpe} {spec} -module {TOP_MODULE}".format(tpe=power_spec_arg, spec=power_spec_file, TOP_MODULE=top_module))

        # Set options
        # pre-elaboration
        verbose_append("set_db leakage_power_effort low")
        verbose_append("set_db lp_insert_clock_gating true")

        # Elaborate the design
        verbose_append("elaborate {TOP_MODULE}".format(TOP_MODULE=top_module))

        # Generate and read the SDCs
        sdc_files = self.generate_sdc_files()  # type: List[str]
        verbose_append("read_sdc {}".format(" ".join(sdc_files)))

        # TODO change effort?
        verbose_append("power_map -root {} -effort low".format(top_module))

        # reading stimulus
        # TODO add time interval based on cycle info; add to defaults.yml to control?
        waveforms = self.get_setting("power.inputs.waveforms")
        for wave in waveforms:
            #verbose_append("read_stimulus {VCD} -dut_instance {TB}/{DUT} -format vcd -frame_count 5 -append".format(VCD=wave, TB=tb_name, DUT=tb_dut))
            verbose_append("read_stimulus {VCD} -dut_instance {TB}/{DUT} -format vcd -cycles 1 /adder/clk -append".format(VCD=wave, TB=tb_name, DUT=tb_dut))

        saifs = self.get_setting("power.inputs.saifs")
        for saif in saifs:
            verbose_append("read_stimulus {SAIF} -dut_instance {TB}/{DUT} -format saif -append".format(SAIF=saif, TB=tb_name, DUT=tb_dut))

        return True

    def run_joules(self) -> bool:
        verbose_append = self.verbose_append

        report_file = os.path.join(self.run_dir, "power_report.out")

        verbose_append("compute_power -mode time_based")

        verbose_append("report_power -by_hierarchy -frames /stim#1/frame#\[0:\] -indent_inst -unit mW -out {FILE} -append".format(FILE=report_file))

        #verbose_append("report_power -frame {/stim#1/frame#\[1:4\]} -by_hierarchy -indent_inst -unit mW")

        #verbose_append("report_power -frame /stim#1/frame#0 -indent_inst -unit mW -out {FILE} -append -csv".format(FILE=report_file))
        #verbose_append("report_power -frame /stim#1/frame#1 -indent_inst -unit mW -out {FILE} -append -csv".format(FILE=report_file))
        #verbose_append("report_power -frame /stim#1/frame#2 -indent_inst -unit mW -out {FILE} -append -csv".format(FILE=report_file))

        #verbose_append("report_power -frame {/stim#1/frame#1} -by_hierarchy -indent_inst -unit mW")
        #verbose_append("report_power -frame {/stim#1/frame#2} -by_hierarchy -indent_inst -unit mW")
        #verbose_append("report_power -frame {/stim#1/frame#3} -by_hierarchy -indent_inst -unit mW")
        #verbose_append("report_power -frame {/stim#1/frame#4} -by_hierarchy -indent_inst -unit mW")
        #verbose_append("report_power -frame {/stim#1/frame#5} -by_hierarchy -indent_inst -unit mW")

        """Close out the power script and run Joules"""
        # Quit Joules
        verbose_append("exit")

        # Create power analysis script
        joules_tcl_filename = os.path.join(self.run_dir, "joules.tcl")

        with open(joules_tcl_filename, "w") as f:
            f.write("\n".join(self.output))

        # Build args
        args = [
            self.get_setting("power.joules.joules_bin"),
            "-files", joules_tcl_filename,
            "-common_ui"
        ]

        HammerVLSILogging.enable_colour = False
        HammerVLSILogging.enable_tag = False

        self.run_executable(args, cwd=self.run_dir)

        HammerVLSILogging.enable_colour = True
        HammerVLSILogging.enable_tag = True

        return True



tool = Joules
