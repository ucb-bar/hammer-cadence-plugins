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

        corners = self.get_mmmc_corners()
        if MMMCCornerType.Extra in list(map(lambda corner: corner.type, corners)):
            for corner in corners:
                if corner.type is MMMCCornerType.Extra:
                    verbose_append("read_libs {EXTRA_LIB} -infer_memory_cells".format(EXTRA_LIB=self.get_timing_libs(corner)))
                    break
        else:
            for corner in corners:
                if corner.type is MMMCCornerType.Setup:
                    verbose_append("read_libs {SETUP_LIBS} -infer_memory_cells".format(SETUP_LIB=self.get_timing_libs(corner)))
                    break

        return True

    def init_design(self) -> bool:
        verbose_append = self.verbose_append

        top_module = self.get_setting("power.inputs.top_module")
        tb_name = self.tb_name
        # Replace . to / formatting in case argument passed from sim tool
        tb_dut = self.tb_dut.replace(".", "/")


        if self.level == FlowLevel.RTL:
            #input_files = self.get_setting("power.inputs.input_files")
            # Read in the design files
            #verbose_append("read_hdl -sv {}".format(" ".join(input_files)))
            verbose_append("read_hdl -sv {}".format(" ".join(self.input_files)))

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
        elif self.level == FlowLevel.GateLevel:
            # Read in the synthesized netlist
            verbose_append("read_netlist {}".format(" ".join(self.input_files)))

            # Read in the post-synth SDCs
            verbose_append("read_sdc {}".format(self.sdc))
        else:
            self.logger.error("Unsupported FlowLevel")
            return False

        report_power_commands = []

        # Generate average power report for all waveforms
        waveforms = self.waveforms
        for waveform in waveforms:
            verbose_append("read_stimulus -file {WAVE} -dut_instance {TB}/{DUT} -alias {WAVE_NAME} -append".format(WAVE=waveform, TB=tb_name, DUT=tb_dut, WAVE_NAME=os.path.basename(waveform)))

        # Generate Specified and Custom Reports
        reports = self.get_power_report_configs()

        for i, report in enumerate(reports):
            waveform = os.path.basename(report.waveform_path)

            module_str = ""
            if report.module:
                module_str = "-module " + report.module

            levels_str = ""
            if report.levels:
                levels_str = "-levels " + str(report.levels)

            stime_str = ""
            if report.start_time:
                stime_ns = report.start_time.value_in_units("ns")
                stime_str += "-start " + stime_ns

            etime_str = ""
            if report.end_time:
                etime_ns = report.end_time.value_in_units("ns")
                etime_str += "-end " + etime_ns

            toggle_signal_str = ""
            if report.toggle_signal:
                if report.num_toggles:
                    toggle_signal_str = "-cycles {NUM} {SIGNAL}".format(NUM=str(report.num_toggles), SIGNAL=report.toggle_signal)
                else:
                    self.logger.error("Must specify the number of toggles if the toggle signal is specified.")
                    return False

            frame_count_str = ""
            if report.frame_count:
                frame_count_str = "-frame_count " + str(report.frame_count)

            stim_alias = waveform + "_" + str(i)
            report_name = ""
            if report.report_name:
                report_name = report.report_name
            else:
                report_name = stim_alias + ".report"

            verbose_append("read_stimulus -file {WAVE_PATH} -dut_instance {TB}/{DUT} {START} {END} {TOGGLE_SIGNAL} -alias {STIM_ALIAS} -append".format(
                WAVE_PATH=report.waveform_path,
                TB=tb_name,
                DUT=tb_dut,
                START=stime_str,
                END=etime_str,
                TOGGLE_SIGNAL=toggle_signal_str,
                STIM_ALIAS=stim_alias
                ))

            # Generate the report commands here
            # Then append them later
            #report_power_commands.append("set num_frames [get_sdb_frames -stims {STIM_ALIAS} -count]".format(STIM_ALIAS=stim_alias))
            #report_power -frames [get_sdb_frames joules.vcd_0] -collate none -cols total -by_hierarchy  -levels 1 -indent_inst -unit mW -out full_test_report.report -append
            #report_power_commands.append("report_power -stims {STIM_ALIAS} -frames {{/{STIM_ALIAS}/frame#[1:$num_frames]}}  -cols total -by_hierarchy {MODULE} {LEVELS} -indent_inst -unit mW -out {REPORT_NAME}".format(
            report_power_commands.append("report_power -frames [get_sdb_frames {STIM_ALIAS}] -collate none -cols total -by_hierarchy {MODULE} {LEVELS} -indent_inst -unit mW -out {REPORT_NAME}".format(
                STIM_ALIAS=stim_alias,
                REPORT_NAME=report_name,
                MODULE=module_str,
                LEVELS=levels_str))


        saifs = self.get_setting("power.inputs.saifs")
        saif_report_commands = []
        for saif in saifs:
            saif_basename = os.path.basename(saif)
            verbose_append("read_stimulus {SAIF} -dut_instance {TB}/{DUT} -format saif -alias {NAME} -append".format(SAIF=saif, TB=tb_name, DUT=tb_dut, NAME=saif_basename))
            saif_report_commands.append("report_power -stims {SAIF} -indent_inst -unit mW -out {SAIF}.report".format(SAIF=saif_basename))


        if self.level == FlowLevel.RTL:
            # Generate and read the SDCs
            sdc_files = self.generate_sdc_files()  # type: List[str]
            verbose_append("read_sdc {}".format(" ".join(sdc_files)))
            verbose_append("set_db auto_super_thread 1")
            verbose_append("syn_power -effort low")

        verbose_append("compute_power -mode time_based")

        verbose_append("report_power -stims {WAVEFORMS} -indent_inst -unit mW -append -out waveforms.report".format(WAVEFORMS=" ".join(list(map(os.path.basename, waveforms)))))

        for cmd in report_power_commands:
            verbose_append(cmd)

        for cmd in saif_report_commands:
            verbose_append(cmd)

        custom_reports = self.get_setting("power.inputs.custom_reports")
        for custom_report in custom_reports:
            verbose_append(custom_report)

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
