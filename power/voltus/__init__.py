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
from hammer_vlsi import HammerPowerTool, HammerToolStep, MMMCCornerType, TimeValue
from hammer_logging import HammerVLSILogging
import hammer_tech

import sys
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)),"../../common"))
from tool import CadenceTool


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
            self.active_power,
            # self.static_rail,
            self.active_rail,
            self.run_voltus
        ])

    # TODO (daniel) library characterization
    def init_technology(self) -> bool:
        return True

    def init_design(self) -> bool:
        verbose_append = self.verbose_append

        verbose_append("set_multi_cpu_usage -local_cpu {}".format(self.get_setting("vlsi.core.max_threads")))

        innovus_db = os.path.join(os.getcwd(), self.par_database)
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

        corners = self.get_mmmc_corners()
        if corners:
            setup_view_names = [] # type: List[str]
            hold_view_names = [] # type: List[str]
            extra_view_names = [] # type: List[str]
            rc_corners = [] # type: List[str]
            for corner in corners:
                # Setting up views for all defined corner types: setup, hold, extra
                if corner.type is MMMCCornerType.Setup:
                    corner_name = "{n}.{t}".format(n=corner.name, t="setup")
                    setup_view_names.append("{n}_view".format(n=corner_name))
                elif corner.type is MMMCCornerType.Hold:
                    corner_name = "{n}.{t}".format(n=corner.name, t="hold")
                    hold_view_names.append("{n}_view".format(n=corner_name))
                elif corner.type is MMMCCornerType.Extra:
                    corner_name = "{n}.{t}".format(n=corner.name, t="extra")
                    extra_view_names.append("{n}_view".format(n=corner_name))
                else:
                    raise ValueError("Unsupported MMMCCornerType")
                rc_corners.append("{n}_rc".format(n=corner_name))

            # Apply analysis views
            # TODO: should not need to analyze extra views as well. Defaulting to hold for now (min. runtime impact).
            verbose_append("set_analysis_view -setup {{ {setup_views} }} -hold {{ {hold_views} {extra_views} }}".format(
                setup_views=" ".join(setup_view_names),
                hold_views=" ".join(hold_view_names),
                extra_views=" ".join(extra_view_names)
            ))
            # Match spefs with corners. Ordering must match (ensured here by get_mmmc_corners())!
            for (spef, rc_corner) in zip(self.spefs, rc_corners):
                verbose_append("read_spef {spef} -rc_corner {corner}".format(spef=os.path.join(os.getcwd(), spef), corner=rc_corner))

        else:
            # TODO: remove hardcoded my_view string
            analysis_view_name = "my_view"
            verbose_append("set_analysis_view -setup {{ {setup_view} }} -hold {{ {hold_view} }}".format(
                setup_view=analysis_view_name,
                hold_view=analysis_view_name
            ))
            verbose_append("read_spef " + os.path.join(os.getcwd(), self.spefs[0]))

        return True

    def static_power(self) -> bool:
        verbose_append = self.verbose_append

        verbose_append("set_db power_method static")
        verbose_append("set_db power_write_static_currents true")
        verbose_append("set_db power_write_db true")

        # Report based on MMMC mode
        corners = self.get_mmmc_corners()
        extra_corners_only = self.get_setting("power.inputs.extra_corners_only")
        if not corners:
            if extra_corners_only:
                self.logger.warning("power.inputs.extra_corners_only not valid in non-MMMC mode! Reporting static power for default analysis view only.")
            verbose_append("report_power -out_dir staticPowerReports")
        else:
            if extra_corners_only:
                extra_corners = list(filter(lambda c: c.type is MMMCCornerType.Extra, corners))
                if len(extra_corners) == 0:
                    self.logger.warning("power.inputs.extra_corners_only is true but no extra MMMC corners specified! Ignoring for static power.")
                else:
                    corners = extra_corners
            for corner in corners:
                # Setting up views for all defined corner types: setup, hold, extra
                if corner.type is MMMCCornerType.Setup:
                    view_name = "{c}.setup_view".format(c=corner.name)
                elif corner.type is MMMCCornerType.Hold:
                    view_name = "{c}.hold_view".format(c=corner.name)
                elif corner.type is MMMCCornerType.Extra:
                    view_name = "{c}.extra_view".format(c=corner.name)
                else:
                    raise ValueError("Unsupported MMMCCornerType")
                verbose_append("report_power -view {VIEW} -out_dir staticPowerReports.{VIEW}".format(VIEW=view_name))

        return True

    def active_power(self) -> bool:
        verbose_append = self.verbose_append

        # Active Vectorless Power Analysis
        verbose_append("set_db power_method dynamic_vectorless")
        # TODO (daniel) add the resolution as an option?
        verbose_append("set_dynamic_power_simulation -resolution 500ps")

        # Check MMMC mode
        corners = self.get_mmmc_corners()
        extra_corners_only = self.get_setting("power.inputs.extra_corners_only")
        if not corners:
            if extra_corners_only:
                self.logger.warning("power.inputs.extra_corners_only not valid in non-MMMC mode! Reporting active power for default analysis view only.")
            verbose_append("report_power -out_dir activePowerReports")
        else:
            if extra_corners_only:
                extra_corners = list(filter(lambda c: c.type is MMMCCornerType.Extra, corners))
                if len(extra_corners) == 0:
                    self.logger.warning("power.inputs.extra_corners_only is true but no extra MMMC corners specified! Ignoring for active power.")
                else:
                    corners = extra_corners
            for corner in corners:
                # Setting up views for all defined corner types: setup, hold, extra
                if corner.type is MMMCCornerType.Setup:
                    view_name = "{c}.setup_view".format(c=corner.name)
                elif corner.type is MMMCCornerType.Hold:
                    view_name = "{c}.hold_view".format(c=corner.name)
                elif corner.type is MMMCCornerType.Extra:
                    view_name = "{c}.extra_view".format(c=corner.name)
                else:
                    raise ValueError("Unsupported MMMCCornerType")
                verbose_append("report_power -view {VIEW} -out_dir activePowerReports.{VIEW}".format(VIEW=view_name))

        # TODO (daniel) deal with different tb/dut hierarchies
        tb_name = self.get_setting("power.inputs.tb_name")
        tb_dut = self.get_setting("power.inputs.tb_dut")
        tb_scope = "{}/{}".format(tb_name, tb_dut)

        # TODO: These times should be either auto calculated/read from the inputs or moved into the same structure as a tuple
        start_times = self.get_setting("power.inputs.start_times")
        end_times = self.get_setting("power.inputs.end_times")

        # Active Vectorbased Power Analysis
        verbose_append("set_db power_method dynamic_vectorbased")
        for vcd_path, vcd_stime, vcd_etime in zip(self.waveforms, start_times, end_times):
            stime_ns = TimeValue(vcd_stime).value_in_units("ns")
            etime_ns = TimeValue(vcd_etime).value_in_units("ns")
            verbose_append("read_activity_file -reset -format VCD {VCD_PATH} -start {stime}ns -end {etime}ns -scope {TESTBENCH}".format(VCD_PATH=os.path.join(os.getcwd(), vcd_path), TESTBENCH=tb_scope, stime=stime_ns, etime=etime_ns))
            vcd_file = os.path.basename(vcd_path)
            # Report based on MMMC mode
            if not corners:
                verbose_append("report_power -out_dir activePower.{VCD_FILE}".format(VCD_FILE=vcd_file))
            else:
                for corner in corners:
                    # Setting up views for all defined corner types: setup, hold, extra
                    if corner.type is MMMCCornerType.Setup:
                        view_name = "{c}.setup_view".format(c=corner.name)
                    elif corner.type is MMMCCornerType.Hold:
                        view_name = "{c}.hold_view".format(c=corner.name)
                    elif corner.type is MMMCCornerType.Extra:
                        view_name = "{c}.extra_view".format(c=corner.name)
                    else:
                        raise ValueError("Unsupported MMMCCornerType")
                    verbose_append("report_power -view {VIEW} -out_dir activePowerReports.{VCD_FILE}.{VIEW}".format(VIEW=view_name, VCD_FILE=vcd_file))

            verbose_append("report_vector_profile -detailed_report true -out_file activePowerProfile.{VCD_FILE}".format(VCD_FILE=vcd_file))

        verbose_append("set_db power_method dynamic")
        for saif_path in self.saifs:
            verbose_append("set_dynamic_power_simulation -reset")
            verbose_append("read_activity_file -reset -format SAIF {SAIF_PATH} -scope {TESTBENCH}".format(SAIF_PATH=os.path.join(os.getcwd(), saif_path), TESTBENCH=tb_scope))
            saif_file=".".join(saif_path.split('/')[-2:])
            # Report based on MMMC mode
            if not corners:
                verbose_append("report_power -out_dir activePower.{SAIF_FILE}".format(SAIF_FILE=saif_file))
            else:
                for corner in corners:
                    # Setting up views for all defined corner types: setup, hold, extra
                    if corner.type is MMMCCornerType.Setup:
                        view_name = "{c}.setup_view".format(c=corner.name)
                    elif corner.type is MMMCCornerType.Hold:
                        view_name = "{c}.hold_view".format(c=corner.name)
                    elif corner.type is MMMCCornerType.Extra:
                        view_name = "{c}.extra_view".format(c=corner.name)
                    else:
                        raise ValueError("Unsupported MMMCCornerType")
                    verbose_append("report_power -view {VIEW} -out_dir activePowerReports.{SAIF_FILE}.{VIEW}".format(VIEW=view_name, SAIF_FILE=saif_file))

        return True

    def static_rail(self) -> bool:
        verbose_append = self.verbose_append

        # TODO (daniel) add more setting parameters
        verbose_append("set_rail_anaylsis_config -analysis_view VIEW -method era_static -accuracy xd -extraction_techfile QRC_TECHFILE")
        verbose_append("set_power_data -format current {FILE NAMES}")
        verbose_append("report_rail -output_dir staticRailReports -type domain AO")

        return True

    def active_rail(self) -> bool:
        return True

    def run_voltus(self) -> bool:
        verbose_append = self.verbose_append

        """Close out the power script and run Voltus"""
        # Quit Voltus
        verbose_append("exit")

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
