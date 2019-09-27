#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  hammer-vlsi plugin for Cadence Genus.
#
#  See LICENSE for licence details.

from hammer_vlsi import HammerToolStep, HierarchicalMode
from hammer_utils import VerilogUtils
from hammer_vlsi import CadenceTool
from hammer_vlsi import HammerSynthesisTool
from hammer_logging import HammerVLSILogging
from hammer_vlsi import MMMCCornerType
import hammer_tech

from typing import Dict, List, Any, Optional

import os
import json
import textwrap
import shutil

class Genus(HammerSynthesisTool, CadenceTool):
    @property
    def post_synth_sdc(self) -> Optional[str]:
        # No post-synth SDC input for synthesis...
        return None

    @property
    def env_vars(self) -> Dict[str, str]:
        new_dict = dict(super().env_vars)
        new_dict["GENUS_BIN"] = self.get_setting("synthesis.genus.genus_bin")
        return new_dict

    def get_release_files(self) -> Dict[str, Any]:
        """return the files released by this tool for the given stage"""
        return {
            netlist:    self.release_netlist,
            sdc:        self.release_sdc,
            sdf:        self.release_sdf_path,
            regs_json:  self.release_regs_json
        }

    def make_release(self) -> None:
        """copies necessary files from run_dir to release_dir"""
        if not os.path.isfile(self.output_netlist) or \
                not os.path.isfile(self.output_sdc) or \
                not os.path.isfile(self.output_sdf) or \
                not os.path.isfile(self.output_regs_json):
            self.error("trying to release with missing output files!")

        copyfile(self.output_netlist,   self.release_netlist)
        copyfile(self.output_sdc,       self.release_sdc)
        copyfile(self.output_sdf,       self.release_sdf)
        copyfile(self.output_regs_json, self.release_regs_json)

    def tool_config_prefix(self) -> str:
        return "synthesis.genus"

    @property
    def steps(self) -> List[HammerToolStep]:
        return self.make_steps_from_methods([
            self.init_environment,
            self.syn_generic,
            self.syn_map,
            self.write_regs,
            self.generate_reports,
            self.write_outputs
        ])

    def do_pre_steps(self, first_step: HammerToolStep) -> bool:
        assert super().do_pre_steps(first_step)
        # If the first step isn't init_environment, then reload from a checkpoint.
        self.append("\n"+"#"*78)
        self.append("# Starting stage: {}".format(first_step.name))
        self.append("#"*78+"\n")
        if first_step.name != "init_environment":
            self.append_tcl(
                "source pre_{step}.script.tcl".format(step=first_step.name))
            self.append_tcl(
                "read_db pre_{step}.db".format(step=first_step.name))
        return True

    def do_between_steps(self, prev: HammerToolStep, next: HammerToolStep) -> bool:
        assert super().do_between_steps(prev, next)
        # Write a checkpoint to disk.
        self.verbose_append_wrap(["write_db", 
            "-all_root_attributes",
            "-script pre_{step}.setup.tcl".format(step=next.name),
            "pre_{step}.db".format(step=next.name)])
        self.append("#"*78)
        self.append("# Starting stage: {}".format(next.name))
        self.append("#"*78)
        return True

        return True

    def do_post_steps(self) -> bool:
        assert super().do_post_steps()
        return self.run_genus()

    @property
    def output_netlist(self) -> str:
        # ssteffl TODO: move to release area, handle separate release step
        return "/{}.mapped.v".format(self.top_module)
    @property
    def mapped_v_path(self) -> str:
        return "/{}.mapped.v".format(self.top_module)

    @property
    def mapped_hier_v_path(self) -> str:
        return "genus_invs_des/genus.v.gz"

    @property
    def mapped_sdc_path(self) -> str:
        return "{}.mapped.sdc".format(self.top_module)

    @property
    def mapped_all_regs_path(self) -> str:
        return "find_regs.json"

    @property
    def output_sdf_path(self) -> str:
        return "{top}.mapped.sdf".format(top=self.top_module)

    @property
    def ran_write_outputs(self) -> bool:
        """The write_outputs stage sets this to True if it was run."""
        return self.attr_getter("_ran_write_outputs", False)

    @ran_write_outputs.setter
    def ran_write_outputs(self, val: bool) -> None:
        self.attr_setter("_ran_write_outputs", val)

    def remove_hierarchical_submodules_from_file(self, path: str) -> str:
        """
        Remove any hierarchical submodules' implementation from the given Verilog source file in path, if it is present.
        If it is not, return the original path.
        :param path: Path to verilog source file
        :return: A path to a modified version of the original file without the given module, or the same path as before.
        """
        with open(path, "r") as f:
            source = f.read()
        submodules = list(map(lambda ilm: ilm.module, self.get_input_ilms()))

        touched = False

        for submodule in submodules:
            if VerilogUtils.contains_module(source, submodule):
                source = VerilogUtils.remove_module(source, submodule)
                touched = True

        if touched:
            # Write the modified input to a new file in run_dir.
            name, ext = os.path.splitext(os.path.basename(path))
            new_filename = str(name) + "_no_submodules" + str(ext)
            new_path = os.path.join(self.run_dir, new_filename)
            with open(new_path, "w") as f:
                f.write(source)
            return new_path
        else:
            return path

    def init_environment(self) -> bool:
        self.create_enter_script()

        # Python sucks here for verbosity
        verbose_append_wrap = self.verbose_append_wrap

        # Generic Settings
        verbose_append_wrap("set_db hdl_error_on_blackbox true")
        verbose_append_wrap("set_db max_cpus_per_server {}".format(self.get_setting("vlsi.core.max_threads")))

        # Clock gating setup
        if self.get_setting("synthesis.clock_gating_mode") == "auto":
            verbose_append_wrap("set_db lp_clock_gating_infer_enable  true")
            # Innovus will create instances named CLKGATE_foo, CLKGATE_bar, etc.
            verbose_append_wrap("set_db lp_clock_gating_prefix  {CLKGATE}")
            verbose_append_wrap("set_db lp_insert_clock_gating  true")
            verbose_append_wrap("set_db lp_clock_gating_hierarchical true")
            verbose_append_wrap("set_db lp_insert_clock_gating_incremental true")
            verbose_append_wrap("set_db lp_clock_gating_register_aware true")

        # Set up libraries.
        # Read timing libraries.
        # ssteffl: don't use absolute paths to things in this run-dir!
        mmmc_path = "./mmmc.tcl"
        with open(mmmc_path, "w") as f:
            f.write(self.generate_mmmc_script())
        verbose_append_wrap("read_mmmc {mmmc_path}".format(mmmc_path=mmmc_path))

        if self.hierarchical_mode.is_nonleaf_hierarchical():
            # Read ILMs.
            for ilm in self.get_input_ilms():
                # Assumes that the ILM was created by Innovus (or at least the file/folder structure).
                verbose_append_wrap(["read_ilm",
                    "-basename {data_dir}/{module}_postRoute"
                        .format(data_dir=ilm.data_dir, module=ilm.module),
                    "-module_name {module}".format(module=ilm.module)])

        # Read LEF layouts.
        if self.is_physical:
            lef_files = self.technology.read_libs([
                hammer_tech.filters.lef_filter
            ], hammer_tech.HammerTechnologyUtils.to_plain_item)
            if self.hierarchical_mode.is_nonleaf_hierarchical():
                ilm_lefs = list(map(lambda ilm: ilm.lef, self.get_input_ilms()))
                lef_files.extend(ilm_lefs)
        else:
            lef_files = self.technology.read_libs([
                hammer_tech.filters.tech_lef_filter
            ], hammer_tech.HammerTechnologyUtils.to_plain_item)
        verbose_append_wrap(["read_physical -lef"] + lef_files)

        # Load input files and check that they are all Verilog.
        if not self.check_input_files([".v", ".sv"]):
            return False
        # We are switching working directories and Genus still needs to find paths.
        abspath_input_files = list(map(lambda name: os.path.join(os.getcwd(), name), self.input_files))  # type: List[str]

        # If we are in hierarchical, we need to remove hierarchical sub-modules/sub-blocks.
        if self.hierarchical_mode.is_nonleaf_hierarchical():
            abspath_input_files = list(map(self.remove_hierarchical_submodules_from_file, abspath_input_files))

        # Add any verilog_synth wrappers (which are needed in some technologies e.g. for SRAMs) which need to be
        # synthesized.
        abspath_input_files += self.technology.read_libs([
            hammer_tech.filters.verilog_synth_filter
        ], hammer_tech.HammerTechnologyUtils.to_plain_item)

        # Read the RTL.
        rtl_filelist = self.get_setting("synthesis.inputs.rtl_filelist")
        if len(rtl_filelist) > 0:
            verbose_append_wrap("read_hdl -f {}".format(rtl_filelist))
        else:
            verbose_append_wrap(["read_hdl"]+abspath_input_files)

        # Elaborate/parse the RTL.
        verbose_append_wrap("elaborate {}".format(self.top_module))
        # Preserve submodules
        if self.hierarchical_mode.is_nonleaf_hierarchical():
            for ilm in self.get_input_ilms():
                verbose_append_wrap("set_db module:{top}/{mod} .preserve true".format(top=self.top_module, mod=ilm.module))
        verbose_append_wrap("init_design -top {}".format(self.top_module))

        # Prevent floorplanning targets from getting flattened.
        # TODO: is there a way to track instance paths through the synthesis process?
        verbose_append_wrap("set_db root: .auto_ungroup none")

        # Set units to pF and technology time unit.
        # Must be done after elaboration.
        verbose_append_wrap("set_units -capacitance 1.0pF")
        verbose_append_wrap("set_load_unit -picofarads 1")
        verbose_append_wrap("set_units -time 1.0{}".format(self.get_time_unit().value_prefix + self.get_time_unit().unit))

        # Set "don't use" cells.
        for l in self.generate_dont_use_commands():
            self.append(l)

        return True

    def syn_generic(self) -> bool:
        self.verbose_append_wrap("syn_generic")
        return True

    def syn_map(self) -> bool:
        self.verbose_append_wrap("syn_map")
        return True

    def generate_reports(self) -> bool:
        """Generate reports."""
        # [ssteffl]: TODO: filter out unnecessary reports here.
        self.verbose_append_wrap(["write_reports",
            "-directory reports",
            "-tag final"])
        for corner in self.get_mmmc_corners():
            if corner.type is MMMCCornerType.Setup:
                view_name = "{cname}.setup_view".format(cname=corner.name)
                self.verbose_append_wrap(["report_timing",
                    "-views", view_name,
                    "-path_type summary",
                    "-split_delay",
                    "-output_format text",
                    "-file reports/final-setup-summary.rpt"])
                self.verbose_append_wrap(["report_timing",
                    "-views", view_name,
                    "-lint",
                    "-output_format text",
                    "-file reports/final-setup-lint.rpt"])
                if self.is_physical:
                    self.verbose_append_wrap(["report_timing",
                        "-views", view_name,
                        "-path_type full_clock",
                        "-split_delay",
                        "-physical",
                        "-output_format text",
                        "-file reports/final-setup-physical.rpt"])
                self.verbose_append_wrap(["report_timing",
                    "-views", view_name,
                    "-path_type full_clock",
                    "-split_delay",
                    "-nets",
                    "-nworst 10",
                    "-lint",
                    "-fields 'timing_point flags arc edge cell fanout load " +\
                        "transition delay arrivalapin_location wire_length'",
                    "-output_format text",
                    "-file reports/final-setup.rpt"])
        return True

    def write_regs(self) -> bool:
        """write regs info to be read in for simulation register forcing"""
        self.verbose_append_wrap(textwrap.dedent('''
        # dump {seq_cells: [], reg_paths: []}
            set write_regs_ir "./find_regs.json"
            set write_regs_ir [open $write_regs_ir "w"]
            puts $write_regs_ir "\{"
            puts $write_regs_ir {   "seq_cells" : [}

            set refs [get_db [get_db lib_cells -if .is_flop==true] .base_name]

            set len [llength $refs]

            for {set i 0} {$i < [llength $refs]} {incr i} {
                if {$i == $len - 1} {
                    puts $write_regs_ir "    \\"[lindex $refs $i]\\""
                } else {
                    puts $write_regs_ir "    \\"[lindex $refs $i]\\","
                }
            }

            puts $write_regs_ir "  \],"
            puts $write_regs_ir {   "reg_paths" : [}

            set regs [get_db [all_registers -edge_triggered -output_pins] .name]

            set len [llength $regs]

            for {set i 0} {$i < [llength $regs]} {incr i} {
                #regsub -all {/} [lindex $regs $i] . myreg
                set myreg [lindex $regs $i]
                if {$i == $len - 1} {
                    puts $write_regs_ir "    \\"$myreg\\""
                } else {
                    puts $write_regs_ir "    \\"$myreg\\","
                }
            }

            puts $write_regs_ir "  \]"

            puts $write_regs_ir "\}"
            close $write_regs_ir
            '''))

        return True

    def write_outputs(self) -> bool:
        verbose_append_wrap = self.verbose_append_wrap
        top = self.top_module

        verbose_append_wrap("write_hdl > {}".format(self.mapped_v_path))
        verbose_append_wrap("write_script > {}.mapped.scr".format(top))
        corners = self.get_mmmc_corners()
        for corner in corners:
            if corner.type is MMMCCornerType.Setup:
                view_name = "{}.setup_view".format(corner.name)
                verbose_append_wrap(["write_sdc",
                    "-view {}".format(view_name),
                    "> {}".format(self.mapped_sdc_path)])

        # We just get "Cannot trace ILM directory. Data corrupted."
        # -hierarchical needs to be used for non-leaf modules
        # self.hierarchical_mode != HierarchicalMode.Flat
        is_hier = self.hierarchical_mode != HierarchicalMode.Leaf 
        verbose_append_wrap(["write_design",
            "-innovus {}".format("-hierarchical" if is_hier else ""),
            "-gzip_files {}".format(top)])

        verbose_append_wrap(["write_sdf",
            "> {top}.mapped.sdf".format(top=top)])

        self.ran_write_outputs = True

        return True

    def run_genus(self) -> bool:
        verbose_append_wrap = self.verbose_append_wrap

        """Close out the synthesis script and run Genus."""
        # Quit Genus.
        verbose_append_wrap("quit")

        # Create synthesis script.
        syn_tcl_filename = os.path.join(self.run_dir, "syn.tcl")

        with open(syn_tcl_filename, "w") as f:
            f.write("\n".join(self.output))

        # Build args.
        args = [
            self.get_setting("synthesis.genus.genus_bin"),
            "-abort_on_error",
            "-f", syn_tcl_filename,
            "-no_gui"
        ]

        if bool(self.get_setting("synthesis.genus.generate_only")):
            self.logger.info("Generate-only mode: command-line is " + " ".join(args))
        else:
            self.run_executable(args, cwd=self.run_dir)
        return True


tool = Genus
