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
from hammer_vlsi import HammerPowerTool, HammerToolStep, MMMCCornerType, FlowLevel, TimeValue
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
        top_module = self.get_setting("power.inputs.top_module")
        tb_name = self.tb_name
        # Replace . to / formatting in case argument passed from sim tool
        tb_dut = self.tb_dut.replace(".", "/")


        if self.level == FlowLevel.RTL:
            hdl = self.get_setting("power.inputs.hdl")
            # Read in the design files
            verbose_append("read_hdl {}".format(" ".join(hdl)))

        elif self.level == FlowLevel.GateLevel:
            syn_db = self.get_setting("power.inputs.database")
            # Read in the synthesis db
            verbose_append("read_db {}".format(syn_db))

        # Setup the power specification
        power_spec_arg = self.map_power_spec_name()
        power_spec_file = self.create_power_spec()

        verbose_append("read_power_intent -{tpe} {spec} -module {TOP_MODULE}".format(tpe=power_spec_arg, spec=power_spec_file, TOP_MODULE=top_module))

        # Set options pre-elaboration
        verbose_append("set_db leakage_power_effort low")
        verbose_append("set_db lp_insert_clock_gating true")


        if self.level == FlowLevel.RTL:
            # Elaborate the design
            verbose_append("elaborate {TOP_MODULE}".format(TOP_MODULE=top_module))

            # Generate and read the SDCs
            sdc_files = self.generate_sdc_files()  # type: List[str]
            verbose_append("read_sdc {}".format(" ".join(sdc_files)))

            verbose_append("power_map -root {} -effort low".format(top_module))

        #verbose_append("gen_clock_tree -clock_root /top/clock1 -name myCT")

        stims = [] # type: List[str]
        framed_stims = [] # type: List[str]

        # Reading stimulus
        waveforms = self.get_setting("power.inputs.waveforms")
        for wave in waveforms:
            wave_basename = os.path.basename(wave)
            stims.append(wave_basename)
            verbose_append("read_stimulus {VCD} -dut_instance {TB}/{DUT} -format vcd -alias {NAME} -append".format(VCD=wave, TB=tb_name, DUT=tb_dut, NAME=wave_basename))

            frames_mode = self.get_setting("power.inputs.frames.mode")
            if frames_mode != "none":
                framed_stims.append(wave_basename)
                if frames_mode == "count":
                    frame_count = str(self.get_setting("power.inputs.frames.frame_count"))
                    verbose_append("read_stimulus {VCD} -dut_instance {TB}/{DUT} -format vcd -frame_count {COUNT} -alias {NAME}_framed -append".format(VCD=wave, TB=tb_name, DUT=tb_dut, COUNT=frame_count, NAME=wave_basename))
                elif frames_mode == "cycles":
                    signal = self.get_setting("power.inputs.frames.toggle_signal_path")
                    cycles = str(self.get_setting("power.inputs.frames.toggle_signal_cycles"))
                    verbose_append("read_stimulus {VCD} -dut_instance {TB}/{DUT} -format vcd -cycles {COUNT} {SIGNAL} -alias {NAME}_framed -append".format(VCD=wave, TB=tb_name, DUT=tb_dut, COUNT=cycles, SIGNAL=signal, NAME=wave_basename))
                else:
                    # TODO throw error?
                    pass

        saifs = self.get_setting("power.inputs.saifs")
        for saif in saifs:
            saif_basename = os.path.basename(saif)
            stims.append(saif_basename)
            verbose_append("read_stimulus {SAIF} -dut_instance {TB}/{DUT} -format saif -alias {NAME} -append".format(SAIF=saif, TB=tb_name, DUT=tb_dut, NAME=saif_basename))


        verbose_append("compute_power -mode time_based")

        for stim in stims:
            # TODO: got rid of -append so the report is overwritten; check functionality
            verbose_append("report_power -stims {STIM} -by_hierarchy -levels 3 -indent_inst -unit mW -out {STIM}.report".format(STIM=stim))

        for stim in framed_stims:
            verbose_append("set num_frames [get_sdb_frames -stims {} -count]".format(stim))
            #verbose_append("puts $num_frames")
            #verbose_append("report_power -stims {NAME} -by_hierarchy -levels 3 -indent_inst -unit mW -out {NAME}.report -append".format(NAME=stim))
            self.append("""
for {{set i 0}} {{$i < $num_frames}} {{incr i}} {{
    report_power -by_hierarchy -cols total -indent_inst -frames /{STIM}/frame#$i -unit mW -out {STIM}.report -append
}}
            """.format(STIM=stim))



        # num hierarchy levels, csv, cols

        #    verbose_append("read_stimulus {VCD} -dut_instance {TB}/{DUT} -format vcd -cycles 1 /ChipTop/clock -append".format(VCD=wave, TB=tb_name, DUT=tb_dut))
        #report_file = os.path.join(self.run_dir, "power_report.out")
        #verbose_append("report_power -by_hierarchy -levels 5 -frames /stim#1/frame#[0:$num_frames] -indent_inst -unit mW -out {FILE} -append".format(FILE=report_file))

        return True

    def run_joules(self) -> bool:
        verbose_append = self.verbose_append

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
