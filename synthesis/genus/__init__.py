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


class Genus(HammerSynthesisTool, CadenceTool):
    @property
    def post_synth_sdc(self) -> Optional[str]:
        # No post-synth SDC input for synthesis...
        return None

    def fill_outputs(self) -> bool:
        # Check that the mapped.v exists if the synthesis run was successful
        # TODO: move this check upwards?
        if not self.ran_write_outputs:
            self.logger.info("Did not run write_outputs")
            return True

        mapped_v = self.mapped_hier_v_path if self.hierarchical_mode.is_nonleaf_hierarchical() else self.mapped_v_path
        if not os.path.isfile(mapped_v):
            raise ValueError("Output mapped verilog %s not found" % (mapped_v)) # better error?
        self.output_files = [mapped_v]

        if not os.path.isfile(self.mapped_sdc_path):
            raise ValueError("Output SDC %s not found" % (self.mapped_sdc_path)) # better error?
        self.output_sdc = self.mapped_sdc_path

        if not os.path.isfile(self.mapped_all_regs_path):
            raise ValueError("Output find_regs.json %s not found" % (self.mapped_all_regs_path))

        with open(self.mapped_all_regs_path, "r") as f:
            j = json.load(f)
            self.output_seq_cells = j["seq_cells"]
            reg_paths = j["reg_paths"]
            for i in range(len(reg_paths)):
                split = reg_paths[i].split("/")
                if split[-2][-1] == "]":
                    split[-2] = "\\" + split[-2]
                    reg_paths[i] = {"path" : '/'.join(split[0:len(split)-1]), "pin" : split[-1]}
                else:
                    reg_paths[i] = {"path" : '/'.join(split[0:len(split)-1]), "pin" : split[-1]}
            self.output_all_regs = reg_paths

        if not os.path.isfile(self.output_sdf_path):
            raise ValueError("Output SDF %s not found" % (self.output_sdf_path))

        self.sdf_file = self.output_sdf_path

        return True

    @property
    def env_vars(self) -> Dict[str, str]:
        new_dict = dict(super().env_vars)
        new_dict["GENUS_BIN"] = self.get_setting("synthesis.genus.genus_bin")
        return new_dict

    def export_config_outputs(self) -> Dict[str, Any]:
        outputs = dict(super().export_config_outputs())
        # TODO(edwardw): find a "safer" way of passing around these settings keys.
        outputs["synthesis.outputs.sdc"] = self.output_sdc
        outputs["synthesis.outputs.seq_cells"] = self.output_seq_cells
        outputs["synthesis.outputs.all_regs"] = self.output_all_regs
        outputs["synthesis.outputs.sdf_file"] = self.output_sdf_path
        return outputs

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
        if first_step.name != "init_environment":
            self.verbose_append("read_db pre_{step}".format(step=first_step.name))
        return True

    def do_between_steps(self, prev: HammerToolStep, next: HammerToolStep) -> bool:
        assert super().do_between_steps(prev, next)
        # Write a checkpoint to disk.
        self.verbose_append("write_db -to_file pre_{step}".format(step=next.name))
        return True

    def do_post_steps(self) -> bool:
        assert super().do_post_steps()
        return self.run_genus()

    @property
    def mapped_v_path(self) -> str:
        return os.path.join(self.run_dir, "{}.mapped.v".format(self.top_module))

    @property
    def mapped_hier_v_path(self) -> str:
        return os.path.join(self.run_dir, "genus_invs_des/genus.v.gz")

    @property
    def mapped_sdc_path(self) -> str:
        return os.path.join(self.run_dir, "{}.mapped.sdc".format(self.top_module))

    @property
    def mapped_all_regs_path(self) -> str:
        return os.path.join(self.run_dir, "find_regs.json")

    @property
    def output_sdf_path(self) -> str:
        return os.path.join(self.run_dir, "{top}.mapped.sdf".format(top=self.top_module))

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
        verbose_append = self.verbose_append

        # Generic Settings
        verbose_append("set_db hdl_error_on_blackbox true")
        verbose_append("set_db max_cpus_per_server {}".format(self.get_setting("vlsi.core.max_threads")))

        # Clock gating setup
        if self.get_setting("synthesis.clock_gating_mode") == "auto":
            verbose_append("set_db lp_clock_gating_infer_enable  true")
            # Innovus will create instances named CLKGATE_foo, CLKGATE_bar, etc.
            verbose_append("set_db lp_clock_gating_prefix  {CLKGATE}")
            verbose_append("set_db lp_insert_clock_gating  true")
            verbose_append("set_db lp_clock_gating_hierarchical true")
            verbose_append("set_db lp_insert_clock_gating_incremental true")
            verbose_append("set_db lp_clock_gating_register_aware true")

        # Set up libraries.
        # Read timing libraries.
        mmmc_path = os.path.join(self.run_dir, "mmmc.tcl")
        with open(mmmc_path, "w") as f:
            f.write(self.generate_mmmc_script())
        verbose_append("read_mmmc {mmmc_path}".format(mmmc_path=mmmc_path))

        if self.hierarchical_mode.is_nonleaf_hierarchical():
            # Read ILMs.
            for ilm in self.get_input_ilms():
                # Assumes that the ILM was created by Innovus (or at least the file/folder structure).
                verbose_append("read_ilm -basename {data_dir}/{module}_postRoute -module_name {module}".format(
                    data_dir=ilm.data_dir, module=ilm.module))

        # Read LEF layouts.
        lef_files = self.technology.read_libs([
            hammer_tech.filters.lef_filter
        ], hammer_tech.HammerTechnologyUtils.to_plain_item)
        if self.hierarchical_mode.is_nonleaf_hierarchical():
            ilm_lefs = list(map(lambda ilm: ilm.lef, self.get_input_ilms()))
            lef_files.extend(ilm_lefs)
        verbose_append("read_physical -lef {{ {files} }}".format(
            files=" ".join(lef_files)
        ))

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
        verbose_append("read_hdl {{ {} }}".format(" ".join(abspath_input_files)))

        # Elaborate/parse the RTL.
        verbose_append("elaborate {}".format(self.top_module))
        # Preserve submodules
        if self.hierarchical_mode.is_nonleaf_hierarchical():
            for ilm in self.get_input_ilms():
                verbose_append("set_db module:{top}/{mod} .preserve true".format(top=self.top_module, mod=ilm.module))
        verbose_append("init_design -top {}".format(self.top_module))

        # Prevent floorplanning targets from getting flattened.
        # TODO: is there a way to track instance paths through the synthesis process?
        verbose_append("set_db root: .auto_ungroup none")

        # Set units to pF and ns.
        # Must be done after elaboration.
        verbose_append("set_units -capacitance 1.0pF")
        verbose_append("set_load_unit -picofarads 1")
        verbose_append("set_units -time 1.0ns")

        # Set "don't use" cells.
        for l in self.generate_dont_use_commands():
            self.append(l)

        return True

    def syn_generic(self) -> bool:
        self.verbose_append("syn_generic")
        return True

    def syn_map(self) -> bool:
        self.verbose_append("syn_map")
        return True

    def generate_reports(self) -> bool:
        """Generate reports."""
        # TODO: extend report generation capabilities
        self.verbose_append("write_reports -directory reports -tag final")
        return True

    def write_regs(self) -> bool:
        """write regs info to be read in for simulation register forcing"""
        self.append('''
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
        ''')

        return True

    def write_outputs(self) -> bool:
        verbose_append = self.verbose_append
        top = self.top_module

        verbose_append("write_hdl > {}".format(self.mapped_v_path))
        verbose_append("write_script > {}.mapped.scr".format(top))
        # TODO: remove hardcoded my_view string
        view_name = "my_view"
        corners = self.get_mmmc_corners()
        for corner in corners:
            if corner.type is MMMCCornerType.Setup:
                view_name = "{cname}.setup_view".format(cname=corner.name)
        verbose_append("write_sdc -view {view} > {file}".format(view=view_name, file=self.mapped_sdc_path))

        # We just get "Cannot trace ILM directory. Data corrupted."
        # -hierarchical needs to be used for non-leaf modules
        is_hier = self.hierarchical_mode != HierarchicalMode.Leaf # self.hierarchical_mode != HierarchicalMode.Flat
        verbose_append("write_design -innovus {hier_flag} -gzip_files {top}".format(
            hier_flag="-hierarchical" if is_hier else "", top=top))

        verbose_append("write_sdf > {run_dir}/{top}.mapped.sdf".format(run_dir=self.run_dir, top=top))

        self.ran_write_outputs = True

        return True

    def run_genus(self) -> bool:
        verbose_append = self.verbose_append

        """Close out the synthesis script and run Genus."""
        # Quit Genus.
        verbose_append("quit")

        # Create synthesis script.
        syn_tcl_filename = os.path.join(self.run_dir, "syn.tcl")

        with open(syn_tcl_filename, "w") as f:
            f.write("\n".join(self.output))

        # Build args.
        args = [
            self.get_setting("synthesis.genus.genus_bin"),
            "-f", syn_tcl_filename,
            "-no_gui"
        ]

        if bool(self.get_setting("synthesis.genus.generate_only")):
            self.logger.info("Generate-only mode: command-line is " + " ".join(args))
        else:
            # Temporarily disable colours/tag to make run output more readable.
            # TODO: think of a more elegant way to do this?
            HammerVLSILogging.enable_colour = False
            HammerVLSILogging.enable_tag = False
            self.run_executable(args, cwd=self.run_dir) # TODO: check for errors and deal with them
            HammerVLSILogging.enable_colour = True
            HammerVLSILogging.enable_tag = True

        return True


tool = Genus
