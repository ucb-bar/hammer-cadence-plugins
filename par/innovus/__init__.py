#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  hammer-vlsi plugin for Cadence Innovus.
#
#  See LICENSE for licence details.

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

    # TODO(johnwright): this should come from the IR
    # ucb-bar/hammer-cad-plugins#30
    def ground_net_name(self):
        return "VSS"

    # TODO(johnwright): this should come from the IR
    # ucb-bar/hammer-cad-plugins#30
    def power_net_name(self):
        return "VDD"


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
        self.power_nets = [self.power_net_name()]
        self.ground_nets = [self.ground_net_name()]
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
            self.place_tap_cells,
            self.power_straps,
            self.place_opt_design,
            self.clock_tree,
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

        # Setup power settings from cpf/upf
        for l in self.generate_power_spec_commands():
            verbose_append(l)

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

    def place_tap_cells(self) -> bool:
        # By default, do nothing
        self.logger.warning("You have not overridden place_tap_cells. By default this step does nothing; you may have trouble with power strap creation later.")
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

    def clock_tree(self) -> bool:
        """Setup and route a clock tree for clock nets."""
        if len(self.get_clock_ports()) > 0:
            # Ignore clock tree when there are no clocks
            self.verbose_append("create_clock_tree_spec")
            self.verbose_append("ccopt_design -hold -report_dir hammer_cts_debug -report_prefix hammer_cts")
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

        power_straps_mode = str(self.get_setting("par.power_straps_mode"))
        if power_straps_mode == "manual":
            power_straps_script_contents = str(self.get_setting("par.power_straps_script_contents"))
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
                        output.append("create_route_blockage -layers {layers} -spacing 0 -{area_flag} {{{x} {y} {urx} {ury}}}".format(
                            x=constraint.x,
                            y=constraint.y,
                            urx=constraint.x+constraint.width,
                            ury=constraint.y+constraint.height,
                            area_flag="rects" if self.version() >= self.version_number("181") else "area",
                            layers="all" if constraint.layers is None else " ".join(get_or_else(constraint.layers, []))
                        ))
                    if ObstructionType.Power in obs_types:
                        output.append("create_route_blockage -pg_nets -layers {layers} -{area_flag} {{{x} {y} {urx} {ury}}}".format(
                            x=constraint.x,
                            y=constraint.y,
                            urx=constraint.x+constraint.width,
                            ury=constraint.y+constraint.height,
                            area_flag="rects" if self.version() >= self.version_number("181") else "area",
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
        method = self.get_setting("par.generate_power_straps_method")
        if method == "by_tracks":
            # By default put straps everywhere
            bbox = None # type: Optional[List[float]]
            weights = [1] # TODO this will change when implementing multiple power domains
            layers = self.get_setting("par.generate_power_straps_options.by_tracks.strap_layers")
            return self.specify_all_power_straps_by_tracks(layers, self.ground_net_name(), [self.power_net_name()], weights, bbox)
        else:
            raise NotImplementedError("Power strap generation method %s is not implemented" % method)

    def specify_std_cell_power_straps(self, bbox: Optional[List[float]], nets: List[str]) -> List[str]:
        """
        Generate a list of TCL commands that build the low-level standard cell power strap rails.
        This will use the -master option to create power straps based on technology.core.tap_cell_rail_reference.
        The layer is set by technology.core.std_cell_rail_layer, which should be the highest metal layer in the std cell rails.

        :param bbox: The optional (2N)-point bounding box of the area to generate straps. By default the entire core area is used.
        :param nets: A list of power net names (e.g. ["VDD", "VSS"]). Currently only two are supported.
        :return: A list of TCL commands that will generate power straps on rails.
        """
        assert len(nets) == 2, "FIXME, this function has only been tested to work with 2 nets (a power and a ground)"
        layer_name = self.get_setting("technology.core.std_cell_rail_layer")
        layer = self.get_stackup().get_metal(layer_name)
        results = ["# Power strap definition for layer %s (rails):\n" % layer_name]
        results.extend([
            "reset_db -category add_stripes"
        ])
        tapcell = self.get_setting("technology.core.tap_cell_rail_reference")
        options = [
            "-pin_layer", layer_name,
            "-layer", layer_name,
            "-over_pins", "1",
            "-master", "\"{}\"".format(tapcell),
            "-block_ring_bottom_layer_limit", layer_name,
            "-block_ring_top_layer_limit", layer_name,
            "-pad_core_ring_bottom_layer_limit", layer_name,
            "-pad_core_ring_top_layer_limit", layer_name,
            "-direction", str(layer.direction),
            "-width", "pin_width",
            "-nets", "{ %s }" % " ".join(nets)
        ]
        if bbox is not None:
            options.extend([
                "-area", "{ %s }" % " ".join(map(lambda x: "%f" % x, bbox))
            ])
        results.append("add_stripes " + " ".join(options) + "\n")
        return results

    def specify_power_straps(self, layer_name: str, bottom_via_layer_name: str, blockage_spacing: float, pitch: float, width: float, spacing: float, offset: float, bbox: Optional[List[float]], nets: List[str], add_pins: bool) -> List[str]:
        """
        Generate a list of TCL commands that will create power straps on a given layer.
        This is a low-level, cad-tool-specific API. It is designed to be called by higher-level methods, so calling this directly is not recommended.
        This method assumes that power straps are built bottom-up, starting with standard cell rails.

        :param layer_name: The layer name of the metal on which to create straps.
        :param bottom_via_layer_name: The layer name of the lowest metal layer down to which to drop vias.
        :param blockage_spacing: The minimum spacing between the end of a strap and the beginning of a macro or blockage.
        :param pitch: The pitch between groups of power straps (i.e. from left edge of strap A to the next left edge of strap A).
        :param width: The width of each strap in a group.
        :param spacing: The spacing between straps in a group.
        :param offset: The offset to start the first group.
        :param bbox: The optional (2N)-point bounding box of the area to generate straps. By default the entire core area is used.
        :param nets: A list of power nets to create (e.g. ["VDD", "VSS"], ["VDDA", "VSS", "VDDB"],  ... etc.).
        :param add_pins: True if pins are desired on this layer; False otherwise.
        :return: A list of TCL commands that will generate power straps.
        """
        # TODO check that this has been not been called after a higher-level metal and error if so
        # TODO warn if the straps are off-pitch
        results = ["# Power strap definition for layer %s:\n" % layer_name]
        results.extend([
            "reset_db -category add_stripes",
            "set_db add_stripes_stacked_via_top_layer {}".format(layer_name),
            "set_db add_stripes_stacked_via_bottom_layer {}".format(bottom_via_layer_name),
            "set_db add_stripes_trim_antenna_back_to_shape {stripe}",
            "set_db add_stripes_spacing_from_block {}".format(blockage_spacing)
        ])
        layer = self.get_stackup().get_metal(layer_name)
        options = [
            "-create_pins", ("1" if (add_pins) else "0"),
            "-block_ring_bottom_layer_limit", layer_name,
            "-block_ring_top_layer_limit", bottom_via_layer_name,
            "-direction", str(layer.direction),
            "-layer", layer_name,
            "-nets", "{%s}" % " ".join(nets),
            "-pad_core_ring_bottom_layer_limit", bottom_via_layer_name,
            "-set_to_set_distance", "%f" % pitch,
            "-spacing", "%f" % spacing,
            "-switch_layer_over_obs", "0",
            "-width", "%f" % width
        ]
        # Where to get the io-to-core offset from a bbox
        index = 0
        if layer.direction == hammer_tech.RoutingDirection.Horizontal:
            index = 1
        elif layer.direction != hammer_tech.RoutingDirection.Vertical:
            raise ValueError("Cannot handle routing direction {d} for layer {l} when creating power straps".format(d=str(layer.direction), l=layer_name))

        if bbox is not None:
            options.extend([
                "-area", "{ %s }" % " ".join(map(lambda x: "%f" % x, bbox)),
                "-start", "%f" % (offset + bbox[index])
            ])

        else:
            # Just put straps in the core area
            options.extend([
                "-area", "[get_db designs .core_bbox]",
                "-start", "[expr [lindex [lindex [get_db designs .core_bbox] 0] %d] + %f]" % (index, offset)
            ])
        results.append("add_stripes " + " ".join(options) + "\n")
        return results

    def specify_power_straps_by_tracks(self, layer_name: str, bottom_via_layer: str, blockage_spacing: float, track_pitch: int, track_width: int, track_spacing: int, track_start: int, track_offset: float, bbox: Optional[List[float]], nets: List[str], add_pins: bool) -> List[str]:
        """
        Generate a list of TCL commands that will create power straps on a given layer by specifying the desired track consumption.
        This method assumes that power straps are built bottom-up, starting with standard cell rails.

        :param layer_name: The layer name of the metal on which to create straps.
        :param bottom_via_layer_name: The layer name of the lowest metal layer down to which to drop vias.
        :param blockage_spacing: The minimum spacing between the end of a strap and the beginning of a macro or blockage.
        :param track_pitch: The integer pitch between groups of power straps (i.e. from left edge of strap A to the next left edge of strap A) in units of the routing pitch.
        :param track_width: The desired number of routing tracks to consume by a single power strap.
        :param track_spacing: The desired number of USABLE routing tracks between power straps. It is recommended to leave this at 0 except to fix DRC issues.
        :param track_start: The index of the first track to start using for power straps relative to the bounding box.
        :param bbox: The optional (2N)-point bounding box of the area to generate straps. By default the entire core area is used.
        :param nets: A list of power nets to create (e.g. ["VDD", "VSS"], ["VDDA", "VSS", "VDDB"], ... etc.).
        :param add_pins: True if pins are desired on this layer; False otherwise.
        :return: A list of TCL commands that will generate power straps.
        """
        # Note: even track_widths will be snapped to a half-track
        layer = self.get_stackup().get_metal(layer_name)
        pitch = track_pitch * layer.pitch
        width = 0.0
        spacing = 0.0
        strap_offset = 0.0
        if track_spacing == 0:
            width, spacing, strap_start = layer.get_width_spacing_start_twwt(track_width, force_even=True)
        else:
            width, spacing, strap_start = layer.get_width_spacing_start_twt(track_width)
            spacing = 2*spacing + track_spacing * layer.pitch - layer.min_width
        offset = track_offset + track_start * layer.pitch + strap_start
        return self.specify_power_straps(layer_name, bottom_via_layer, blockage_spacing, pitch, width, spacing, offset, bbox, nets, add_pins)

    # TODO(johnwright) there's nothing innovus-specific about this, so these APIs should be moved to core hammer
    # ucb-bar/hammer-cad-plugins#57
    def specify_all_power_straps_by_tracks(self, layer_names: List[str], ground_net: str, power_nets: List[str], power_weights: List[int], bbox: Optional[List[float]]) -> List[str]:
        """
        Generate a list of TCL commands that will create power straps on a given set of layers by specifying the desired per-track track consumption and utilization.
        This will build standard cell power strap rails first. Layer-specific parameters are read from the hammer config:
            - par.generate_power_straps_options.by_tracks.blockage_spacing
            - par.generate_power_straps_options.by_tracks.track_width
            - par.generate_power_straps_options.by_tracks.track_spacing
            - par.generate_power_straps_options.by_tracks.power_utilization
        These settings are all overridable by appending an underscore followed by the metal name (e.g. power_utilization_M3).

        :param layer_names: The list of metal layer names on which to create straps.
        :param ground_net: The name of the ground net in this design. Only 1 ground net is supported.
        :param power_nets: A list of power nets to create (not ground). Currently only supports 1 (e.g. ["VDD"]).
        :param power_weights: Specifies the power strap placement pattern for multiple-domain designs (e.g. ["VDDA", "VDDB"] with [2, 1] will produce 2 VDDA straps for ever 1 VDDB strap).
        :param bbox: The optional (2N)-point bounding box of the area to generate straps. By default the entire core area is used.
        :return: A list of TCL commands that will generate power straps.
        """
        assert len(power_nets) == len(power_weights)
        if len(power_nets) > 1:
            raise NotImplementedError("FIXME: I don't yet support multiple power domains")
        # TODO when implementing multiple power domains, this needs to change based on the floorplan
        output = self.specify_std_cell_power_straps(bbox, [ground_net, power_nets[0]])
        bottom_via_layer = self.get_setting("technology.core.std_cell_rail_layer")
        last = self.get_stackup().get_metal(bottom_via_layer)
        for layer_name in layer_names:
            layer = self.get_stackup().get_metal(layer_name)
            assert layer.index > last.index, "Must build power straps bottom-up"
            if last.direction == layer.direction:
                raise ValueError("Layers {a} and {b} run in the same direction, but have no power straps between them.".format(a=last.name, b=layer.name))

            def get_metal_setting(key: str) -> str:
                default = "par.generate_power_straps_options.by_tracks." + key
                override = default + "_" + layer.name
                try:
                    return self.get_setting(override)
                except KeyError:
                    try:
                        return self.get_setting(default)
                    except KeyError:
                        raise ValueError("No value set for key {}".format(default))

            blockage_spacing = float(get_metal_setting("blockage_spacing"))
            track_width = int(get_metal_setting("track_width"))
            track_spacing = int(get_metal_setting("track_spacing"))
            power_utilization = float(get_metal_setting("power_utilization"))

            assert power_utilization > 0.0
            assert power_utilization <= 1.0

            # Calculate how many tracks we consume
            # This strategy uses pairs of power and ground
            consumed_tracks = 2 * track_width + track_spacing
            track_pitch = consumed_tracks / power_utilization

            track_start = 0 # TODO this matters for hierarchical coordination
            offset = layer.offset # TODO this matters for hierarchical coordination
            add_pins = False # TODO this should only be true for hierarchical cells
            nets = [ground_net, power_nets[0]] # TODO this needs to change when implementing multiple power domains
            output.extend(self.specify_power_straps_by_tracks(layer_name, last.name, blockage_spacing, track_pitch, track_width, track_spacing, track_start, offset, bbox, nets, add_pins))
            last = layer
        return output


tool = Innovus
