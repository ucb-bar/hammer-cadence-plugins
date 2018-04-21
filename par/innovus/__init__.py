#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  hammer-vlsi plugin for Cadence Innovus.
#
#  Copyright 2018 Edward Wang <edward.c.wang@compdigitec.com>

from typing import List, Dict, Optional

import os

from hammer_vlsi import HammerPlaceAndRouteTool, CadenceTool, HammerVLSILogging, HammerToolStep, PlacementConstraintType


# Notes: camelCase commands are the old syntax (deprecated)
# snake_case commands are the new/common UI syntax.
# This plugin should only use snake_case commands.

class Innovus(HammerPlaceAndRouteTool, CadenceTool):
    @property
    def env_vars(self) -> Dict[str, str]:
        v = dict(super().env_vars)
        v["INNOVUS_BIN"] = self.get_setting("par.innovus.innovus_bin")
        return v

    def do_pre_steps(self, first_step: HammerToolStep) -> bool:
        assert super().do_pre_steps(first_step)
        # Restore from the last checkpoint if we're not starting over.
        if first_step.name != "init_design":
            self.verbose_append("read_db pre_{step}".format(step=first_step.name))
        return True

    def do_between_steps(self, prev: HammerToolStep, next: HammerToolStep) -> bool:
        assert super().do_between_steps(prev, next)
        # Write a checkpoint to disk.
        self.verbose_append("write_db pre_{step}".format(step=next.name))
        return True

    def do_post_steps(self) -> bool:
        assert super().do_post_steps()
        return self.run_innovus()

    @property
    def output(self) -> List[str]:
        """
        Buffered output to be put into par.tcl.
        """
        return self.attr_getter("_output", [])

    # Python doesn't have Scala's nice currying syntax (e.g. val newfunc = func(_, fixed_arg))
    def verbose_append(self, cmd: str) -> None:
        self.verbose_tcl_append(cmd, self.output)
    def append(self, cmd: str) -> None:
        self.tcl_append(cmd, self.output)

    @property
    def steps(self) -> List[HammerToolStep]:
        return self.make_steps_from_methods([
            self.init_design,
            self.floorplan_design,
            self.power_straps,
            self.place_opt_design,
            self.route_design,
            self.opt_design,
            self.write_design
        ])

    def init_design(self) -> bool:
        """Initialize the design."""
        self.create_enter_script()

        verbose_append = self.verbose_append

        # Generic settings
        verbose_append("set_db design_process_node {}".format(self.get_setting("vlsi.core.node")))
        verbose_append("set_multi_cpu_usage -local_cpu {}".format(self.get_setting("vlsi.core.max_threads")))

        # Read LEF layouts.
        lef_files = self.read_libs([
            self.lef_filter
        ], self.to_plain_item)
        verbose_append("read_physical -lef {{ {files} }}".format(
            files=" ".join(lef_files)
        ))

        # Read timing libraries.
        mmmc_path = os.path.join(self.run_dir, "mmmc.tcl")
        with open(mmmc_path, "w") as f:
            f.write(self.generate_mmmc_script())
        verbose_append("read_mmmc {mmmc_path}".format(mmmc_path=mmmc_path))

        # Read netlist.
        # Innovus only supports structural Verilog for the netlist.
        if not self.check_input_files([".v"]):
            return False
        # We are switching working directories and Genus still needs to find paths.
        abspath_input_files = list(map(lambda name: os.path.join(os.getcwd(), name), self.input_files))
        verbose_append("read_netlist {{ {files} }} -top {top}".format(
            files=" ".join(abspath_input_files),
            top=self.top_module
        ))

        # Run init_design to validate data and start the Cadence place-and-route workflow.
        verbose_append("init_design")

        # Set design effort.
        verbose_append("set_db design_flow_effort {}".format(self.get_setting("par.innovus.design_flow_effort")))
        return True

    def floorplan_design(self) -> bool:
        floorplan_tcl = os.path.join(self.run_dir, "floorplan.tcl")
        with open(floorplan_tcl, "w") as f:
            f.write("\n".join(self.create_floorplan_tcl()))
        self.verbose_append("source -echo -verbose {}".format(floorplan_tcl))
        return True

    def power_straps(self) -> bool:
        power_straps_tcl = os.path.join(self.run_dir, "power_straps.tcl")
        with open(power_straps_tcl, "w") as f:
            f.write("\n".join(self.create_power_straps_tcl()))
        self.verbose_append("source -echo -verbose {}".format(power_straps_tcl))
        return True

    def place_opt_design(self) -> bool:
        """Place the design and do pre-routing optimization."""
        self.verbose_append("place_opt_design")
        return True

    def route_design(self) -> bool:
        """Route the design."""
        self.verbose_append("route_design")
        return True

    def opt_design(self) -> bool:
        """Post-route optimization and fix setup & hold time violations."""
        self.verbose_append("opt_design -post_route -setup -hold")
        return True

    @property
    def output_innovus_lib_name(self) -> str:
        return "{top}_FINAL".format(top=self.top_module)

    def write_design(self) -> bool:
        # Save the Innovus design.
        self.verbose_append("write_db {lib_name} -def -verilog".format(
            lib_name=self.output_innovus_lib_name
        ))

        # GDS streamout.
        self.verbose_append("write_stream -output_macros -mode ALL -unit 1000 gds_file")
        # extra junk: -map_file -attach_inst_name ... -attach_net_name ...

        # Make sure that generated-scripts exists.
        generated_scripts_dir = os.path.join(self.run_dir, "generated-scripts")
        os.makedirs(generated_scripts_dir, exist_ok=True)

        # Create open_chip script.
        with open(os.path.join(generated_scripts_dir, "open_chip.tcl"), "w") as f:
            f.write("""
        read_db {name}
                """.format(name=self.output_innovus_lib_name))

        with open(os.path.join(generated_scripts_dir, "open_chip"), "w") as f:
            f.write("""
        cd {run_dir}
        source enter
        $INNOVUS_BIN -common_ui -win -files generated-scripts/open_chip.tcl
                """.format(run_dir=self.run_dir))
        self.run_executable([
            "chmod", "+x", os.path.join(generated_scripts_dir, "open_chip")
        ])
        return True

    def run_innovus(self) -> bool:
        # Quit Innovus.
        self.verbose_append("exit")

        # Create par script.
        par_tcl_filename = os.path.join(self.run_dir, "par.tcl")
        with open(par_tcl_filename, "w") as f:
            f.write("\n".join(self.output))

        # Build args.
        args = [
            self.get_setting("par.innovus.innovus_bin"),
            "-nowin",  # Prevent the GUI popping up.
            "-common_ui",
            "-files", par_tcl_filename
        ]

        # Temporarily disable colours/tag to make run output more readable.
        # TODO: think of a more elegant way to do this?
        HammerVLSILogging.enable_colour = False
        HammerVLSILogging.enable_tag = False
        self.run_executable(args, cwd=self.run_dir)  # TODO: check for errors and deal with them
        HammerVLSILogging.enable_colour = True
        HammerVLSILogging.enable_tag = True

        # TODO: check that par run was successful

        return True

    def create_floorplan_tcl(self) -> List[str]:
        """
        Create a floorplan TCL depending on the floorplan mode.
        """
        output = []  # type: List[str]

        floorplan_mode = str(self.get_setting("par.innovus.floorplan_mode"))
        if floorplan_mode == "manual":
            floorplan_script_contents = str(self.get_setting("par.innovus.floorplan_script_contents"))
            # TODO(edwardw): proper source locators/SourceInfo
            output.append("# Floorplan manually specified from HAMMER")
            output.extend(floorplan_script_contents.split("\n"))
        elif floorplan_mode == "generate":
            output.extend(self.generate_floorplan_tcl())
        else:
            if floorplan_mode != "blank":
                self.logger.error("Invalid floorplan_mode {mode}. Using blank floorplan.".format(mode=floorplan_mode))
            # Write blank floorplan
            output.append("# Blank floorplan specified from HAMMER")
        return output

    def create_power_straps_tcl(self) -> List[str]:
        """
        Create power straps TCL commands depending on the mode.
        """
        output = []  # type: List[str]

        power_straps_mode = str(self.get_setting("par.innovus.power_straps_mode"))
        if power_straps_mode == "manual":
            power_straps_script_contents = str(self.get_setting("par.innovus.power_straps_script_contents"))
            # TODO(edwardw): proper source locators/SourceInfo
            output.append("# Power straps script manually specified from HAMMER")
            output.extend(power_straps_script_contents.split("\n"))
        elif power_straps_mode == "generate":
            output.extend(self.generate_power_straps_tcl())
        else:
            if power_straps_mode != "blank":
                self.logger.error(
                    "Invalid power_straps_mode {mode}. Using blank power straps script.".format(mode=power_straps_mode))
            # Write blank floorplan
            output.append("# Blank power straps script specified from HAMMER")
        return output

    @staticmethod
    def generate_chip_size_constraint(width: float, height: float, left: float, bottom: float, right: float,
                                      top: float, site: Optional[str]) -> str:
        """
        Given chip width/height and margins, generate an Innovus TCL command to create the floorplan.
        Also requires a technology specific name for the core site
        """

        if site is None:
            site_str = ""
        else:
            site_str = "-site " + str(site)

        # -flip -f allows standard cells to be flipped correctly during place-and-route
        return ("create_floorplan -core_margins_by die -flip f "
                "-die_size_by_io_height max {site_str} "
                "-die_size {{ {width} {height} {left} {bottom} {right} {top} }}").format(
            site_str=site_str,
            width=width,
            height=height,
            left=left,
            bottom=bottom,
            right=right,
            top=top
        )

    def generate_floorplan_tcl(self) -> List[str]:
        """
        Generate a TCL floorplan for Innovus based on the input config/IR.
        Not to be confused with create_floorplan_tcl, which calls this function.
        """
        output = []  # type: List[str]

        # TODO(edwardw): proper source locators/SourceInfo
        output.append("# Floorplan automatically generated from HAMMER")

        # Top-level chip size constraint.
        # Default/fallback constraints if no other constraints are provided.
        chip_size_constraint = self.generate_chip_size_constraint(
            site=None,
            width=1000.0, height=1000.0,
            left=100, bottom=100, right=100, top=100
        )

        floorplan_constraints = self.get_placement_constraints()
        for constraint in floorplan_constraints:
            # Floorplan names/insts need to not include the top-level module,
            # despite the internal get_db commands including the top-level module...
            # e.g. Top/foo/bar -> foo/bar
            new_path = "/".join(constraint.path.split("/")[1:])

            if new_path == "":
                assert constraint.type == PlacementConstraintType.TopLevel, "Top must be a top-level/chip size constraint"
                margins = constraint.margins
                assert margins is not None
                # Set top-level chip dimensions.
                site = self.get_setting("vlsi.technology.placement_site", "")  # type: Optional[str]
                if site == "":
                    site = None
                chip_size_constraint = self.generate_chip_size_constraint(
                    site=site,
                    width=constraint.width,
                    height=constraint.height,
                    left=margins.left,
                    bottom=margins.bottom,
                    right=margins.right,
                    top=margins.top
                )
            else:
                if constraint.type == PlacementConstraintType.Dummy:
                    pass
                elif constraint.type == PlacementConstraintType.Placement:
                    output.append("create_guide -name {name} -area {x1} {y1} {x2} {y2}".format(
                        name=new_path,
                        x1=constraint.x,
                        x2=constraint.x + constraint.width,
                        y1=constraint.y,
                        y2=constraint.y + constraint.height
                    ))
                elif constraint.type == PlacementConstraintType.HardMacro:
                    output.append("place_inst {inst} {x} {y} {orientation}".format(
                        inst=new_path,
                        x=constraint.x,
                        y=constraint.y,
                        orientation=constraint.orientation if constraint.orientation != None else "r0"
                    ))
                elif constraint.type == PlacementConstraintType.Hierarchical:
                    raise NotImplementedError("Hierarchical not implemented yet")
                else:
                    assert False, "Should not reach here"
        return [chip_size_constraint] + output

    def generate_power_straps_tcl(self) -> List[str]:
        """
        Generate a TCL script to create power straps from the config/IR.
        :return: Power straps TCL script.
        """
        raise NotImplementedError("Not implemented yet")


tool = Innovus
