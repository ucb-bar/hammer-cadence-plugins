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

from hammer_config import HammerJSONEncoder
from hammer_utils import get_or_else, optional_map, coerce_to_grid, check_on_grid, lcm_grid
from hammer_vlsi import HammerPowerTool, HammerToolStep, MMMCCornerType, TimeValue
from hammer_logging import HammerVLSILogging
import hammer_tech
from specialcells import CellType

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
    def tech_lib_dir(self) -> str:
        return os.path.join(self.run_dir, "tech_pgv")

    @property
    def stdcell_lib_dir(self) -> str:
        return os.path.join(self.run_dir, "stdcell_pgv")

    @property
    def macro_lib_dir(self) -> str:
        return os.path.join(self.run_dir, "macro_pgv")

    @property
    def ran_stdcell_pgv(self) -> bool:
        """ init_technology sets this to True if stdcell PG views were generated """
        return self.attr_getter("_ran_stdcell_pgv", False)

    @ran_stdcell_pgv.setter
    def ran_stdcell_pgv(self, val: bool) -> None:
        self.attr_setter("_ran_stdcell_pgv", val)

    @property
    def ran_macro_pgv(self) -> bool:
        """ init_technology sets this to True if macro PG views were generated """
        return self.attr_getter("_ran_macro_pgv", False)

    @ran_macro_pgv.setter
    def ran_macro_pgv(self, val: bool) -> None:
        self.attr_setter("_ran_macro_pgv", val)

    def tech_lib_filter(self) -> List[Callable[[hammer_tech.Library], bool]]:
        """ Filter only libraries from tech plugin """
        return [self.filter_for_tech_libs]

    def filter_for_tech_libs(self, lib: hammer_tech.Library) -> bool:
        return lib in self.technology.tech_defined_libraries

    def extra_lib_filter(self) -> List[Callable[[hammer_tech.Library], bool]]:
        """ Filter only libraries from vlsi.inputs.extra_libraries """
        return [self.filter_for_extra_libs]

    def filter_for_extra_libs(self, lib: hammer_tech.Library) -> bool:
        return lib in list(map(lambda el: el.store_into_library(), self.technology.get_extra_libraries()))

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

    def init_technology(self) -> bool:
        verbose_append = self.verbose_append

        corners = self.get_mmmc_corners()

	# Options for set_pg_library_mode
        base_options = [] # type: List(str)
        if self.get_setting("power.voltus.lef_layer_map"):
            base_options.extend(["-lef_layer_map", self.get_setting("power.voltus.lef_layer_map")])

	# Characterize tech & stdcell libraries only once
        if not os.path.isdir(self.tech_lib_dir) and not os.path.isdir(self.stdcell_lib_dir):
            self.logger.info("Generating techonly and stdcell PG libraries for the first time...")
            # Get only the tech-defined libraries
            tech_lib_lefs = self.technology.read_libs([hammer_tech.filters.lef_filter], hammer_tech.HammerTechnologyUtils.to_plain_item, self.tech_lib_filter())
            verbose_append("read_physical -lef {{ {} }}".format(" ".join(tech_lib_lefs)))

            tech_options = base_options.copy()
            # Append list of fillers
            stdfillers = self.technology.get_special_cell_by_type(CellType.StdFiller)
            if len(stdfillers) > 0:
                stdfillers = list(map(lambda f: str(f), stdfillers[0].name))
                tech_options.extend(["-filler_cells", "{{ {} }} ".format(" ".join(stdfillers))])
            # TODO need separate decap special cells
            #decaps = self.technology.get_special_cell_by_type(CellType.Decap)
            #if len(decaps) > 0:
                #decaps = list(map(lambda d: str(d), decaps[0].name))
                #tech_options.extend(["-decap_cells", "{{ {} }}".format(" ".join(decaps))])

            # TODO deal with no corners case (use default supply voltage + temperature)
            for corner in corners:
                # Start with tech-only library
                options = tech_options.copy()
                options.extend([
                    "-extraction_tech_file", self.get_mmmc_qrc(corner), #TODO: QRC should be tied to stackup
                    "-cell_type", "techonly",
                    "-default_power_voltage", str(corner.voltage.value),
                    "-temperature", str(corner.temp.value)
                ])

                verbose_append("set_pg_library_mode {}".format(" ".join(options)))
                verbose_append("write_pg_library -out_dir {}".format(os.path.join(self.tech_lib_dir, corner.name)))

                # Next do stdcell library
                # TODO: exit if spice model + corners not in tech JSON
                options[options.index("techonly")] = "stdcells"
                # TODO: add spice model + corners properly
                if corner.name == "PVT_0P63V_100C":
                    options.extend(["-spice_models", "/home/ff/ee241/spring21-labs/asap7PDK_r1p5/models/hspice/7nm_SS.pm"])
                else:
                    options.extend(["-spice_models", "/home/ff/ee241/spring21-labs/asap7PDK_r1p5/models/hspice/7nm_FF.pm"])
                    
                verbose_append("set_pg_library_mode {}".format(" ".join(options)))
                verbose_append("write_pg_library -out_dir {}".format(os.path.join(self.stdcell_lib_dir, corner.name)))
                self.ran_stdcell_pgv = True
        else:
            self.logger.info("techonly and stdcell PG libraries already generated, skipping...")
            self.ran_stdcell_pgv = True

	# Characterize macro libraries once, unless list of extra libraries has changed
        extra_lib_lefs = self.technology.read_libs([hammer_tech.filters.lef_filter], hammer_tech.HammerTechnologyUtils.to_plain_item, self.extra_lib_filter())
        extra_lib_lefs_json = os.path.join(self.run_dir, "extra_lib_lefs.json")
        prior_extra_lib_lefs = [] # type: List[str]
        if os.path.exists(extra_lib_lefs_json):
            with open(extra_lib_lefs_json, "r") as f:
                prior_extra_lib_lefs = json.loads(f.read())

        if (not os.path.isdir(self.macro_lib_dir) or extra_lib_lefs != prior_extra_lib_lefs) and self.get_setting("power.voltus.macro_pgv"):
            self.logger.info("Generating macro PG libraries...")
            with open(extra_lib_lefs_json, "w") as f:
                f.write(json.dumps(extra_lib_lefs, cls=HammerJSONEncoder, indent=4))        

            macro_options = base_options.copy()
            macro_options.extend(["-stream_layer_map", self.get_gds_map_file()])

            extra_lib_sp = self.technology.read_libs([hammer_tech.filters.spice_filter], hammer_tech.HammerTechnologyUtils.to_plain_item, self.extra_lib_filter())
            if len(extra_lib_sp) == 0:
                self.logger.error("Must have Spice netlists for macro PG library generation! Skipping.")
                return True
            else:
                macro_options.extend(["spice_subckts", "{{ {} }}".format(" ".join(extra_lib_sp))])

            extra_lib_gds = self.technology.read_libs([hammer_tech.filters.gds_filter], hammer_tech.HammerTechnologyUtils.to_plain_item, self.extra_lib_filter())
            if len(extra_lib_gds) == 0:
                self.logger.error("Must have GDS data for macro PG library generation! Skipping.")
                return True
            else:
                macro_options.extend(["stream_files", "{{ {} }}".format(" ".join(extra_lib_gds))])

            verbose_append("read_physical -lef {{ {} }}".format(" ".join(extra_lib_lefs)))
            
            # TODO deal with no corners case (use default supply voltage + temperature)
            for corner in corners:
                options = macro_options.copy()
                options.extend([
                    "-extraction_tech_file", self.get_mmmc_qrc(corner), #TODO: QRC should be tied to stackup
                    "-cell_type", "macros",
                    "-default_power_voltage", str(corner.voltage.value),
                    "-temperature", str(corner.temp.value),
                ])
                # TODO: add spice model + corners properly
                if corner.name == "PVT_0P63V_100C":
                    options.extend(["-spice_models", "/home/ff/ee241/spring21-labs/asap7PDK_r1p5/models/hspice/7nm_SS.pm"])
                else:
                    options.extend(["-spice_models", "/home/ff/ee241/spring21-labs/asap7PDK_r1p5/models/hspice/7nm_FF.pm"])
                verbose_append("set_pg_library_mode {}".format(" ".join(options)))
                verbose_append("write_pg_library -out_dir {}".format(os.path.join(self.macro_lib_dir, corner.name)))
            self.ran_macro_pgv = True
        elif (os.path.isdir(self.macro_lib_dir) and extra_lib_lefs == prior_extra_lib_lefs):
            self.logger.info("macro PG libraries already generated and macros have not changed, skipping...")
            self.ran_macro_pgv = True

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

        # TODO (daniel) deal with multiple corners
        corners = self.get_mmmc_corners()
        if corners:
            for corner in corners:
                if corner.type is MMMCCornerType.Setup:
                    setup_view_name = "{cname}.setup_view".format(cname=corner.name)
                    setup_spef_name = "{cname}.setup_rc".format(cname=corner.name)
                elif corner.type is MMMCCornerType.Hold:
                    hold_view_name = "{cname}.hold_view".format(cname=corner.name)
                    hold_spef_name = "{cname}.hold_rc".format(cname=corner.name)
            verbose_append("set_analysis_view -setup {SETUP_VIEW} -hold {HOLD_VIEW}".format(SETUP_VIEW=setup_view_name, HOLD_VIEW=hold_view_name))

            # TODO: fixme (this relies on par writing spefs in setup, hold order)
            for (spef, rc_corner) in zip(self.spefs, [setup_spef_name, hold_spef_name]):
                verbose_append("read_spef {spef} -rc_corner {corner}".format(spef=os.path.join(os.getcwd(), spef), corner=rc_corner))
        else:
            verbose_append("read_spef " + os.path.join(os.getcwd(), self.spefs[0]))            

        return True

    def static_power(self) -> bool:
        verbose_append = self.verbose_append

        verbose_append("set_db power_method static")
        verbose_append("set_db power_write_static_currents true")
        verbose_append("set_db power_write_db true")

        # Report based on MMMC mode
        corners = self.get_mmmc_corners()
        if not corners:
            verbose_append("report_power -out_dir staticPowerReports")
        else:
            setup_view_name = "{cname}.setup_view".format(cname=(list(filter(lambda corner: corner.type is MMMCCornerType.Setup, corners)).pop().name))
            verbose_append("report_power -view {SETUP_VIEW} -out_dir staticPowerReports.{SETUP_VIEW}".format(SETUP_VIEW=setup_view_name))
            hold_view_name = "{cname}.hold_view".format(cname=(list(filter(lambda corner: corner.type is MMMCCornerType.Hold, corners)).pop().name))
            verbose_append("report_power -view {HOLD_VIEW} -out_dir staticPowerReports.{HOLD_VIEW}".format(HOLD_VIEW=hold_view_name))

        return True

    def rail_analysis(self, method: str, power_dir: str, output_dir: Optional[str] = None) -> bool:
        """
        Generic method for rail analysis
        params:
        - method: "static" or "dynamic"
        - power_dir: relative path to static or active power current files
        - output_dir: relative path to rail analysis output dir
        """
        verbose_append = self.verbose_append

        if not output_dir:
            output_dir = method + "RailReports"

        # Decide accuracy based on existence of PGV libraries, unless overridden
        accuracy = self.get_setting("power.voltus.rail_accuracy")
        if not accuracy:
            accuracy = "hd" if self.ran_stdcell_pgv else "xd" # hd still works w/o macro PG views
            
        base_options = [
            "-method", method,
            "-accuracy", accuracy,
            "-process_techgen_em_rules", "true",
            "-em_peak_analysis", "true",
            "-enable_rlrp_analysis", "true",
            "-gif_resolution", "high",
            "-verbosity", "true"
        ]
        if method == "static":
            base_options.extend(["-enable_sensitivity_analysis", "true"])

        # TODO: Need combinations of all power nets + voltage domains
        pg_nets = self.get_all_power_nets() + self.get_all_ground_nets()
        # Report based on MMMC corners
        corners = self.get_mmmc_corners()
        if not corners:
            options = base_options.copy()
            pg_libs = [os.path.join(self.tech_lib_dir, "techonly.cl")]
            if self.ran_stdcell_pgv:
                pg_libs.append(os.path.join(self.stdcell_lib_dir, "stdcells.cl"))
            if self.ran_macro_pgv:
                # Assume library name matches cell name
                # TODO: Use some filters w/ LEFUtils to extract cells from LEFs
                macros = list(map(lambda l: l.library.name, self.technology.get_extra_libraries()))
                pg_libs.extend(list(map(lambda l: os.path.join(self.macro_lib_dir, "macros_{}.cl".format(l)), macros)))
            options.extend(["-power_grid_libraries", "{{ {} }}".format(" ".join(pg_libs))])
            verbose_append("set_rail_analysis_config {}".format(" ".join(options)))
            # TODO: get nets and .ptiavg files using TCL from the .ptifiles file in the power reports directory
            power_data = list(map(lambda n: "{POWER_DIR}/{METHOD}_{NET}.ptiavg".format(
                POWER_DIR=power_dir,
                METHOD=method,
                NET=n.name), pg_nets))
            verbose_append("set_power_data -format current {{ {} }}".format(" ".join(power_data)))
            verbose_append("report_rail -output_dir {} -type domain ALL".format(output_dir))
            # TODO: Find highest run number, increment by 1 to enable reporting IRdrop regions
        else:
            for corner in corners:
                options = base_options.copy()
                if corner.type is MMMCCornerType.Setup:
                    view_name = corner.name + ".setup_view"
                elif corner.type is MMMCCornerType.Hold:
                    view_name = corner.name + ".hold_view"
                elif corner.type is MMMCCornerType.Extra:
                    view_name = corner.name + ".extra_view"
                else:
                    raise ValueError("Unsupported MMMCCornerType")
                pg_libs = [os.path.join(self.tech_lib_dir, corner.name, "techonly.cl")]
                if self.ran_stdcell_pgv:
                    pg_libs.append(os.path.join(self.stdcell_lib_dir, corner.name, "stdcells.cl"))
                if self.ran_macro_pgv:
                    # Assume library name matches cell name
                    # TODO: Use some filters w/ LEFUtils to extract cells from LEFs
                    macros = list(map(lambda l: l.library.name, self.technology.get_extra_libraries()))
                    pg_libs.extend(list(map(lambda l: os.path.join(self.macro_lib_dir, corner.name, "macros_{}.cl".format(l)), macros)))

                options.extend([
                    "-power_grid_libraries", "{{ {} }}".format(" ".join(pg_libs)),
                    "-analysis_view", view_name,
                    "-temperature", str(corner.temp.value)
                ])
                verbose_append("set_rail_analysis_config {}".format(" ".join(options)))
                verbose_append("set_power_data -reset")
                # TODO: get nets and .ptiavg files using TCL from the .ptifiles file in the power reports directory
                power_data = list(map(lambda n: "{POWER_DIR}.{VIEW}/{METHOD}_{NET}.ptiavg".format(
                    POWER_DIR=power_dir,
                    VIEW=view_name,
                    METHOD=method,
                    NET=n.name), pg_nets))
                verbose_append("set_power_data -format current {{ {} }}".format(" ".join(power_data)))
                verbose_append("report_rail -output_dir {} -type domain ALL".format(output_dir))
                # TODO: Find highest run number, increment by 1 to enable reporting IRdrop regions

        return True

    def static_rail(self) -> bool:
        return self.rail_analysis("static", "staticPowerReports")

    def active_power(self) -> bool:
        verbose_append = self.verbose_append

        # Active Vectorless Power Analysis
        verbose_append("set_db power_method dynamic_vectorless")
        # TODO (daniel) add the resolution as an option?
        verbose_append("set_dynamic_power_simulation -resolution 500ps")

        # Check MMMC mode
        corners = self.get_mmmc_corners()
        if not corners:
            verbose_append("report_power -out_dir activePowerReports")
        else:
            setup_view_name = "{cname}.setup_view".format(cname=(list(filter(lambda corner: corner.type is MMMCCornerType.Setup, corners)).pop().name))
            hold_view_name = "{cname}.hold_view".format(cname=(list(filter(lambda corner: corner.type is MMMCCornerType.Hold, corners)).pop().name))

        # Report based on MMMC mode
        if not corners:
            verbose_append("report_power -out_dir activePowerReports")
        else:
            verbose_append("report_power -view {SETUP_VIEW} -out_dir activePowerReports.{SETUP_VIEW}".format(SETUP_VIEW=setup_view_name))
            verbose_append("report_power -view {HOLD_VIEW} -out_dir activePowerReports.{HOLD_VIEW}".format(HOLD_VIEW=hold_view_name))

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
                verbose_append("report_power -view {SETUP_VIEW} -out_dir activePower.{VCD_FILE}.{SETUP_VIEW}".format(SETUP_VIEW=setup_view_name, VCD_FILE=vcd_file))
                verbose_append("report_power -view {HOLD_VIEW} -out_dir activePower.{VCD_FILE}.{HOLD_VIEW}".format(HOLD_VIEW=hold_view_name, VCD_FILE=vcd_file))
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
                verbose_append("report_power -view {SETUP_VIEW} -out_dir activePower.{SAIF_FILE}.{SETUP_VIEW}".format(SETUP_VIEW=setup_view_name, SAIF_FILE=saif_file))
                verbose_append("report_power -view {HOLD_VIEW} -out_dir activePower.{SAIF_FILE}.{HOLD_VIEW}".format(HOLD_VIEW=hold_view_name, SAIF_FILE=saif_file))
        return True

    def active_rail(self) -> bool:
        # Vectorless database
        passed = self.rail_analysis("dynamic", "activePowerReports", "activeRailReports")

        # Vectorbased databases
        for vcd_path in self.waveforms:
            passed = self.rail_analysis("dynamic", "activePower." + os.path.basename(vcd_path), "activeRailReports." + os.path.basename(vcd_path))
        for saif_path in self.saifs:
            saif_file=".".join(saif_path.split('/')[-2:])
            passed = self.rail_analysis("dynamic", "activePower." + saif_file, "activeRailReports." + saif_file)
        return passed

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
