#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  hammer-vlsi plugin for Cadence Genus.
#
#  Copyright 2017-2018 Edward Wang <edward.c.wang@compdigitec.com>

from hammer_vlsi import HammerToolStep, VerilogUtils, HierarchicalMode
from hammer_vlsi import CadenceTool
from hammer_vlsi import HammerSynthesisTool
from hammer_vlsi import HammerVLSILogging
from hammer_vlsi import MMMCCornerType

from typing import Dict, List, Any

import os


class Genus(HammerSynthesisTool, CadenceTool):
    def fill_outputs(self) -> bool:
        # Check that the mapped.v exists if the synthesis run was successful
        # TODO: move this check upwards?
        if not self.ran_write_outputs:
            self.logger.info("Did not run write_outputs")
            return True

        mapped_v = self.mapped_v_path
        if not os.path.isfile(mapped_v):
            raise ValueError("Output mapped verilog %s not found" % (mapped_v)) # better error?
        self.output_files = [mapped_v]

        if not os.path.isfile(self.mapped_sdc_path):
            raise ValueError("Output SDC %s not found" % (self.mapped_sdc_path)) # better error?
        self.output_sdc = self.mapped_sdc_path
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
        return outputs

    @property
    def steps(self) -> List[HammerToolStep]:
        return self.make_steps_from_methods([
            self.init_environment,
            self.syn_generic,
            self.syn_map,
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
    def output(self) -> List[str]:
        """
        Buffered output to be put into syn.tcl.
        """
        return self.attr_getter("_output", [])

    @property
    def mapped_v_path(self) -> str:
        return os.path.join(self.run_dir, "{}.mapped.v".format(self.top_module))

    @property
    def mapped_sdc_path(self) -> str:
        return os.path.join(self.run_dir, "{}.mapped.sdc".format(self.top_module))

    @property
    def ran_write_outputs(self) -> bool:
        """The write_outputs stage sets this to True if it was run."""
        return self.attr_getter("_ran_write_outputs", False)

    @ran_write_outputs.setter
    def ran_write_outputs(self, val: bool) -> None:
        self.attr_setter("_ran_write_outputs", val)

    # Python doesn't have Scala's nice currying syntax (e.g. val newfunc = func(_, fixed_arg))
    def verbose_append(self, cmd: str) -> None:
        self.verbose_tcl_append(cmd, self.output)
    def append(self, cmd: str) -> None:
        self.tcl_append(cmd, self.output)

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

        # Set up libraries.
        # Read timing libraries.
        mmmc_path = os.path.join(self.run_dir, "mmmc.tcl")
        with open(mmmc_path, "w") as f:
            f.write(self.generate_mmmc_script())
        verbose_append("read_mmmc {mmmc_path}".format(mmmc_path=mmmc_path))

        if self.hierarchical_mode.is_nonroot_hierarchical():
            # Read ILMs.
            for ilm in self.get_input_ilms():
                # Assumes that the ILM was created by Innovus (or at least the file/folder structure).
                verbose_append(
                    "read_ilm -basename {dir}/{module}_postRoute -module_name {module}".format(dir=ilm.dir,
                                                                                               module=ilm.module))

        # Read LEF layouts.
        lef_files = self.read_libs([
            self.lef_filter
        ], self.to_plain_item)
        if self.hierarchical_mode.is_nonroot_hierarchical():
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
        if self.hierarchical_mode.is_nonroot_hierarchical():
            abspath_input_files = list(map(self.remove_hierarchical_submodules_from_file, abspath_input_files))

        # Read the RTL.
        verbose_append("read_hdl {{ {} }}".format(" ".join(abspath_input_files)))

        # Elaborate/parse the RTL.
        verbose_append("elaborate {}".format(self.top_module))
        verbose_append("init_design -top {}".format(self.top_module))

        # Prevent floorplanning targets from getting flattened.
        # TODO: is there a way to track instance paths through the synthesis process?
        verbose_append("set_db root: .auto_ungroup none")

        # Set units to pF and ns.
        # Must be done after elaboration.
        verbose_append("set_units -capacitance 1.0pF")
        verbose_append("set_load_unit -picofarads 1")
        verbose_append("set_units -time 1.0ns")

        return True

    def syn_generic(self) -> bool:
        self.verbose_append("syn_generic")
        return True

    def syn_map(self) -> bool:
        self.verbose_append("syn_map")
        return True

    def generate_reports(self) -> bool:
        """Generate reports."""
        # TODO: implement report generation
        return True

    def write_outputs(self) -> bool:
        verbose_append = self.verbose_append
        top = self.top_module

        verbose_append("write_hdl > {}".format(self.mapped_v_path))
        verbose_append("write_script > {}.mapped.scr".format(top))
        # TODO: remove hardcoded my_view string
        view_name = "my_view"
        corners = self.get_mmmc_corners()
        if(corners):
            for corner in corners:
                if(corner.type is MMMCCornerType.Setup):
                    view_name = "{cname}.setup_view".format(cname=corner.name)
        verbose_append("write_sdc -view {view} > {file}".format(view=view_name, file=self.mapped_sdc_path))

        # -hierarchical doesn't work for anything but the root
        # We just get "Cannot trace ILM directory. Data corrupted."
        is_hier = self.hierarchical_mode == HierarchicalMode.Root # self.hierarchical_mode != HierarchicalMode.Flat
        verbose_append("write_design -innovus {hier_flag} -gzip_files {top}".format(
            hier_flag="-hierarchical" if is_hier else "", top=top))

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
