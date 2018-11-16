#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  hammer-vlsi plugin for Cadence Innovus.
#
#  Copyright 2018 Edward Wang <edward.c.wang@compdigitec.com>

import shutil
from typing import List, Dict, Optional, Callable, Tuple

import os, errno

from hammer_utils import get_or_else, optional_map
from hammer_vlsi import HammerPlaceAndRouteTool, CadenceTool, HammerToolStep, \
    PlacementConstraintType, HierarchicalMode, ILMStruct, ObstructionType
from hammer_logging import HammerVLSILogging
import hammer_tech


# Notes: camelCase commands are the old syntax (deprecated)
# snake_case commands are the new/common UI syntax.
# This plugin should only use snake_case commands.

class Innovus(HammerPlaceAndRouteTool, CadenceTool):
    def fill_outputs(self) -> bool:
        if self.ran_write_ilm:
            # Check that the ILMs got written.

            ilm_data_dir = "{ilm_dir_name}/mmmc/ilm_data/{top}".format(ilm_dir_name=self.ilm_dir_name,
                                                                       top=self.top_module)
            postRoute_v_gz = os.path.join(ilm_data_dir, "{top}_postRoute.v.gz".format(top=self.top_module))

            if not os.path.isfile(postRoute_v_gz):
                raise ValueError("ILM output postRoute.v.gz %s not found" % (postRoute_v_gz))

            # Copy postRoute.v.gz to postRoute.ilm.v.gz since that's what Genus seems to expect.
            postRoute_ilm_v_gz = os.path.join(ilm_data_dir, "{top}_postRoute.ilm.v.gz".format(top=self.top_module))
            shutil.copyfile(postRoute_v_gz, postRoute_ilm_v_gz)

            # Write output_ilms.
            self.output_ilms = [
                ILMStruct(dir=self.ilm_dir_name, data_dir=ilm_data_dir, module=self.top_module,
                          lef=os.path.join(self.run_dir, "{top}ILM.lef".format(top=self.top_module)))
            ]
        else:
            self.output_ilms = []

        self.output_gds = self.output_gds_filename
        self.output_netlist = self.output_netlist_filename
        # TODO(johnwright): parametrize these
        # ucb-bar/hammer-cad-plugins#30
        self.power_nets = ["VDD"]
        self.ground_nets = ["VSS"]
        self.hcells_list = []
        return True

    @property
    def output_gds_filename(self) -> str:
        return os.path.join(self.run_dir, "{top}.gds".format(top=self.top_module))

    @property
    def output_netlist_filename(self) -> str:
        return os.path.join(self.run_dir, "{top}.lvs.v".format(top=self.top_module))

    @property
    def env_vars(self) -> Dict[str, str]:
        v = dict(super().env_vars)
        v["INNOVUS_BIN"] = self.get_setting("par.innovus.innovus_bin")
        return v

    @property
    def _step_transitions(self) -> List[Tuple[str, str]]:
        """
        Private helper property to keep track of which steps we ran so that we
        can create symlinks.
        This is a list of (pre, post) steps
        """
        return self.attr_getter("__step_transitions", [])

    @_step_transitions.setter
    def _step_transitions(self, value: List[Tuple[str, str]]) -> None:
        self.attr_setter("__step_transitions", value)

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
        self._step_transitions = self._step_transitions + [(prev.name, next.name)]
        return True

    def do_post_steps(self) -> bool:
        assert super().do_post_steps()
        # Create symlinks for post_<step> to pre_<step+1> to improve usability.
        try:
            for prev, next in self._step_transitions:
                os.symlink(
                    os.path.join(self.run_dir, "pre_{next}".format(next=next)), # src
                    os.path.join(self.run_dir, "post_{prev}".format(prev=prev)) # dst
                )
        except OSError as e:
            if e.errno != errno.EEXIST:
                self.logger.warning("Failed to create post_* symlinks: " + str(e))
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
        steps = [
            self.init_design,
            self.floorplan_design,
            self.power_straps,
            self.place_opt_design,
            self.route_design,
            self.opt_design
        ]
        write_design_step = [
            self.write_design
        ]  # type: List[Callable[[], bool]]
        if self.hierarchical_mode == HierarchicalMode.Flat:
            # Nothing to do
            pass
        elif self.hierarchical_mode == HierarchicalMode.Leaf:
            # All modules in hierarchical must write an ILM
            write_design_step += [self.write_ilm]
        elif self.hierarchical_mode == HierarchicalMode.Hierarchical:
            # All modules in hierarchical must write an ILM
            write_design_step += [self.write_ilm]
        elif self.hierarchical_mode == HierarchicalMode.Top:
            # No need to write ILM at the top.
            # Top needs assemble_design instead.
            steps += [self.assemble_design]
            pass
        else:
            raise NotImplementedError("HierarchicalMode not implemented: " + str(self.hierarchical_mode))
        return self.make_steps_from_methods(steps + write_design_step)

    def tool_config_prefix(self) -> str:
        return "par.innovus"

    def init_design(self) -> bool:
        """Initialize the design."""
        self.create_enter_script()

        verbose_append = self.verbose_append

        # Generic settings
        verbose_append("set_db design_process_node {}".format(self.get_setting("vlsi.core.node")))
        verbose_append("set_multi_cpu_usage -local_cpu {}".format(self.get_setting("vlsi.core.max_threads")))

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

        # Read timing libraries.
        mmmc_path = os.path.join(self.run_dir, "mmmc.tcl")
        with open(mmmc_path, "w") as f:
            f.write(self.generate_mmmc_script())
        verbose_append("read_mmmc {mmmc_path}".format(mmmc_path=mmmc_path))

        # Read netlist.
        # Innovus only supports structural Verilog for the netlist; the Verilog can be optionally compressed.
        if not self.check_input_files([".v", ".v.gz"]):
            return False

        # We are switching working directories and we still need to find paths.
        abspath_input_files = list(map(lambda name: os.path.join(os.getcwd(), name), self.input_files))
        verbose_append("read_netlist {{ {files} }} -top {top}".format(
            files=" ".join(abspath_input_files),
            top=self.top_module
        ))

        if self.hierarchical_mode.is_nonleaf_hierarchical():
            # Read ILMs.
            for ilm in self.get_input_ilms():
                # Assumes that the ILM was created by Innovus (or at least the file/folder structure).
                verbose_append("read_ilm -cell {module} -directory {dir}".format(dir=ilm.dir, module=ilm.module))

        # Run init_design to validate data and start the Cadence place-and-route workflow.
        verbose_append("init_design")

        # Set design effort.
        verbose_append("set_db design_flow_effort {}".format(self.get_setting("par.innovus.design_flow_effort")))

        # Set "don't use" cells.
        for l in self.generate_dont_use_commands():
            self.append(l)

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

    def assemble_design(self) -> bool:
        # TODO: implement the assemble_design step.
        return True

    def write_netlist(self) -> bool:
        # Output the Verilog netlist for the design and include physical cells (-phys) like decaps and fill
        # TODO(johnwright): We may want to include a -exclude_insts_of_cells [...] here
        # We may also want to include connect_global_net commands to tie body pins, although that feels like
        # a separate logical step
        self.verbose_append("write_netlist {netlist} -top_module_first -top_module {top} -exclude_leaf_cells -phys -flat".format(
            netlist=self.output_netlist_filename,
            top=self.top_module
        ))
        return True

    def write_gds(self) -> bool:
        map_file = get_or_else(
            optional_map(self.get_gds_map_file(), lambda f: "-map_file {}".format(f)),
            ""
        )

        gds_files = self.technology.read_libs([
            hammer_tech.filters.gds_filter
        ], hammer_tech.HammerTechnologyUtils.to_plain_item)

        # If we are not merging, then we want to use -output_macros.
        # output_macros means that Innovus should take any macros it has and
        # just output the cells into the GDS. These cells will not have physical
        # information inside them and will need to be merged with some other
        # step later. We do not care about uniquifying them because Innovus will
        # output a single cell for each instance (essentially already unique).

        # On the other hand, if we tell Innovus to do the merge then it is going
        # to get a GDS with potentially multiple child cells and we then tell it
        # to uniquify these child cells in case of name collisons. Without that
        # we could have one child that applies to all cells of that name which
        # is often not what you want.
        # For example, if macro ADC1 has a subcell Comparator which is different
        # from ADC2's subcell Comparator, we don't want ADC1's Comparator to
        # replace ADC2's Comparator.
        # Note that cells not present in the GDSes to be merged will be emitted
        # as-is in the output (like with -output_macros).
        merge_options = "-output_macros" if not self.get_setting("par.inputs.gds_merge") else "-uniquify_cell_names -merge {{ {} }}".format(
            " ".join(gds_files)
        )

        # TODO: explanation for why we chose this unit parameter
        self.verbose_append("write_stream -mode ALL -unit 1000 {map_file} {merge_options} {gds}".format(
            map_file=map_file,
            merge_options=merge_options,
            gds=self.output_gds_filename
        ))
        return True

    @property
    def output_innovus_lib_name(self) -> str:
        return "{top}_FINAL".format(top=self.top_module)

    def write_design(self) -> bool:
        # Save the Innovus design.
        self.verbose_append("write_db {lib_name} -def -verilog".format(
            lib_name=self.output_innovus_lib_name
        ))

        # Write netlist
        self.write_netlist()

        # GDS streamout.
        self.write_gds()

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

    @property
    def ran_write_ilm(self) -> bool:
        """The write_ilm stage sets this to True if it was run."""
        return self.attr_getter("_ran_write_ilm", False)

    @ran_write_ilm.setter
    def ran_write_ilm(self, val: bool) -> None:
        self.attr_setter("_ran_write_ilm", val)

    @property
    def ilm_dir_name(self) -> str:
        return os.path.join(self.run_dir, "{top}ILMDir".format(top=self.top_module))

    def write_ilm(self) -> bool:
        """Run time_design and write out the ILM."""
        self.verbose_append("time_design -post_route")
        self.verbose_append("time_design -post_route -hold")
        self.verbose_append("write_lef_abstract -5.8 {top}ILM.lef".format(top=self.top_module))
        self.verbose_append("write_ilm -model_type all -to_dir {ilm_dir_name} -type_flex_ilm ilm".format(
            ilm_dir_name=self.ilm_dir_name))
        self.ran_write_ilm = True
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
                        orientation=constraint.orientation if constraint.orientation is not None else "r0"
                    ))
                elif constraint.type == PlacementConstraintType.Obstruction:
                    obs_types = get_or_else(constraint.obs_types, [])  # type: List[ObstructionType]
                    if ObstructionType.Place in obs_types:
                        output.append("create_place_blockage -area {{{x} {y} {urx} {ury}}}".format(
                            x=constraint.x,
                            y=constraint.y,
                            urx=constraint.x+constraint.width,
                            ury=constraint.y+constraint.height
                        ))
                    if ObstructionType.Route in obs_types:
                        output.append("create_route_blockage -layers {layers} -spacing 0 -area {{{x} {y} {urx} {ury}}}".format(
                            x=constraint.x,
                            y=constraint.y,
                            urx=constraint.x+constraint.width,
                            ury=constraint.y+constraint.height,
                            layers="all" if constraint.layers is None else " ".join(get_or_else(constraint.layers, []))
                        ))
                    if ObstructionType.Power in obs_types:
                        output.append("create_route_blockage -pg_nets -layers {layers} -area {{{x} {y} {urx} {ury}}}".format(
                            x=constraint.x,
                            y=constraint.y,
                            urx=constraint.x+constraint.width,
                            ury=constraint.y+constraint.height,
                            layers="all" if constraint.layers is None else " ".join(get_or_else(constraint.layers, []))
                        ))
                elif constraint.type == PlacementConstraintType.Hierarchical:
                    raise ValueError("Hierarchical should have been resolved and turned into a hard macro by now")
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
