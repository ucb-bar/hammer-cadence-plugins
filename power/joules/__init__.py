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

        verbose_append("read_libs {HOLD_LIB} {SETUP_LIB}".format(HOLD_LIB=hold_lib, SETUP_LIB=setup_lib))

        return True

    def init_design(self) -> bool:
        verbose_append = self.verbose_append

        top_module = self.get_setting("power.inputs.top_module")
        tb_name = self.tb_name
        # Replace . to / formatting in case argument passed from sim tool
        tb_dut = self.tb_dut.replace(".", "/")


        if self.level == FlowLevel.RTL:
            input_files = self.get_setting("power.inputs.input_files")
            # Read in the design files
            verbose_append("read_hdl {}".format(" ".join(input_files)))

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

        elif self.level == FlowLevel.GateLevel:
            #syn_db = self.get_setting("power.inputs.database")
            # Read in the synthesized netlist
            verbose_append("read_netlist {}".format(" ".join(self.input_files)))

            # Read in the post-synth SDCs
            verbose_append("read_sdc {}".format(self.sdc))


        stims = [] # type: List[str]

        reports = self.get_power_report_configs()
        custom_reports = self.get_setting("power.inputs.custom_reports")

        # TODO: These times should be either auto calculated/read from the inputs or moved into the same structure as a tuple
        start_times = self.get_setting("power.inputs.start_times")
        end_times = self.get_setting("power.inputs.end_times")

        # Reading stimulus
        waveforms = self.get_setting("power.inputs.waveforms")
        for i in range(len(waveforms)):
            wave = waveforms[i]
            wave_basename = os.path.basename(wave)
            stims.append(wave_basename)

            # general waveform report
            if not start_times:
                verbose_append("read_stimulus {VCD} -dut_instance {TB}/{DUT} -format vcd -alias {NAME} -append".format(VCD=wave, TB=tb_name, DUT=tb_dut, NAME=wave_basename))
            else:
                stime_ns = TimeValue(start_times[i]).value_in_units("ns")
                #etime_ns = TimeValue(end_times[i]).value_in_units("ns")
                verbose_append("read_stimulus {VCD} -dut_instance {TB}/{DUT} -start {STIME}ns -format vcd -alias {NAME} -append".format(VCD=wave, TB=tb_name, DUT=tb_dut, STIME=stime_ns, NAME=wave_basename))

            #verbose_append("read_stimulus {VCD} -dut_instance {TB}/{DUT} -format vcd -alias {NAME} -append".format(VCD=wave, TB=tb_name, DUT=tb_dut, NAME=wave_basename))

            # specified reports
            report_count = 0
            for report in reports:
                if not start_times:
                    verbose_append("read_stimulus {VCD} -dut_instance {TB}/{DUT} -format vcd -cycles {COUNT} {SIGNAL} -alias {NAME}_{NUM} -append".format(VCD=wave, TB=tb_name,
                        DUT=tb_dut, COUNT=report.num_toggles, SIGNAL=report.toggle_signal, NAME=wave_basename, NUM=str(report_count)))
                else:
                    stime_ns = TimeValue(start_times[i]).value_in_units("ns")
                    #etime_ns = TimeValue(end_times[i]).value_in_units("ns")
                    verbose_append("read_stimulus {VCD} -dut_instance {TB}/{DUT} -format vcd -start {STIME}ns -cycles {COUNT} {SIGNAL} -alias {NAME}_{NUM} -append".format(VCD=wave, TB=tb_name,
                        DUT=tb_dut, STIME=stime_ns, COUNT=report.num_toggles, SIGNAL=report.toggle_signal, NAME=wave_basename, NUM=str(report_count)))

                report_count += 1


        saifs = self.get_setting("power.inputs.saifs")
        for saif in saifs:
            saif_basename = os.path.basename(saif)
            stims.append(saif_basename)
            verbose_append("read_stimulus {SAIF} -dut_instance {TB}/{DUT} -format saif -alias {NAME} -append".format(SAIF=saif, TB=tb_name, DUT=tb_dut, NAME=saif_basename))

        verbose_append("compute_power -mode time_based")

        for stim in stims:
            # TODO: got rid of -append so the report is overwritten; check functionality
            verbose_append("report_power -stims {STIM} -by_hierarchy -levels 3 -indent_inst -unit mW -out {STIM}.report".format(STIM=stim))

            for i in range(len(reports)):
                rpt = reports[i]
                levels = rpt.levels
                # TODO need to add on TB/DUT/module to module name?
                rpt_module = rpt.module
                verbose_append("set num_frames [get_sdb_frames -stims {}_{} -count]".format(stim, i))
                self.append("""
for {{set i 0}} {{$i < $num_frames}} {{incr i}} {{
    report_power -by_hierarchy -levels {LVLS} -cols total -indent_inst -frames /{STIM}_{NUM}/frame#$i -unit mW -out {STIM}_{NUM}.report -append
}}
                """.format(LVLS=levels,STIM=stim, NUM=str(i)))

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
