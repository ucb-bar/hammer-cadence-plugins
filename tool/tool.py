from functools import reduce
from typing import List, Optional, Dict
import os
import json

from hammer_vlsi import HammerTool, HasSDCSupport, HasCPFSupport, HasUPFSupport, TCLTool
from hammer_vlsi.constraints import MMMCCorner
from hammer_utils import optional_map, add_dicts

class CadenceTool(HasSDCSupport, HasCPFSupport, HasUPFSupport, TCLTool, HammerTool):
    """Mix-in trait with functions useful for Cadence-based tools."""

    @property
    def config_dirs(self) -> List[str]:
        # Override this to pull in Cadence-common configs.
        return [self.get_setting("cadence.common_path")] + super().config_dirs

    @property
    def env_vars(self) -> Dict[str, str]:
        """
        Get the list of environment variables required for this tool.
        Note to subclasses: remember to include variables from super().env_vars!
        """
        # Use the base extra_env_variables and ensure that our custom variables are on top.
        list_of_vars = self.get_setting("cadence.extra_env_vars")  # type: List[Dict[str, Any]]
        assert isinstance(list_of_vars, list)

        cadence_vars = {
            "CDS_LIC_FILE": self.get_setting("cadence.CDS_LIC_FILE"),
            "CADENCE_HOME": self.get_setting("cadence.cadence_home")
        }

        return reduce(add_dicts, [dict(super().env_vars)] + list_of_vars + [cadence_vars], {})

    def version_number(self, version: str) -> int:
        """
        Assumes versions look like MAJOR_ISRMINOR and we will have less than 100 minor versions.
        """
        main_version = int(version.split("_")[0]) # type: int
        minor_version = 0 # type: int
        if "_" in version:
            minor_version = int(version.split("_")[1][3:])
        return main_version * 100 + minor_version

    def get_timing_libs(self, corner: Optional[MMMCCorner] = None) -> str:
        """
        Helper function to get the list of ASCII timing .lib files in space separated format.
        Note that Cadence tools support ECSM, so we can use the ECSM-based filter.

        :param corner: Optional corner to consider. If supplied, this will use filter_for_mmmc to select libraries that
        match a given corner (voltage/temperature).
        :return: List of lib files separated by spaces
        """
        pre_filters = optional_map(corner, lambda c: [self.filter_for_mmmc(voltage=c.voltage,
                                                                           temp=c.temp)])  # type: Optional[List[Callable[[hammer_tech.Library],bool]]]

        lib_args = self.technology.read_libs([hammer_tech.filters.timing_lib_with_ecsm_filter],
                                             hammer_tech.HammerTechnologyUtils.to_plain_item,
                                             extra_pre_filters=pre_filters)
        return " ".join(lib_args)

    def get_mmmc_qrc(self, corner: MMMCCorner) -> str:
        lib_args = self.technology.read_libs([hammer_tech.filters.qrc_tech_filter],
                                             hammer_tech.HammerTechnologyUtils.to_plain_item,
                                             extra_pre_filters=[
                                                 self.filter_for_mmmc(voltage=corner.voltage, temp=corner.temp)])
        return " ".join(lib_args)

    def get_qrc_tech(self) -> str:
        """
        Helper function to get the list of rc corner tech files in space separated format.

        :return: List of qrc tech files separated by spaces
        """
        lib_args = self.technology.read_libs([
            hammer_tech.filters.qrc_tech_filter
        ], hammer_tech.HammerTechnologyUtils.to_plain_item)
        return " ".join(lib_args)

    def generate_mmmc_script(self) -> str:
        """
        Output for the mmmc.tcl script.
        Innovus (init_design) requires that the timing script be placed in a separate file.

        :return: Contents of the mmmc script.
        """
        mmmc_output = []  # type: List[str]

        def append_mmmc(cmd: str) -> None:
            self.verbose_tcl_append(cmd, mmmc_output)

        # Create an Innovus constraint mode.
        constraint_mode = "my_constraint_mode"
        sdc_files = []  # type: List[str]

        # Generate constraints
        clock_constraints_fragment = os.path.join(self.run_dir, "clock_constraints_fragment.sdc")
        with open(clock_constraints_fragment, "w") as f:
            f.write(self.sdc_clock_constraints)
        sdc_files.append(clock_constraints_fragment)

        # Generate port constraints.
        pin_constraints_fragment = os.path.join(self.run_dir, "pin_constraints_fragment.sdc")
        with open(pin_constraints_fragment, "w") as f:
            f.write(self.sdc_pin_constraints)
        sdc_files.append(pin_constraints_fragment)

        # Add the post-synthesis SDC, if present.
        post_synth_sdc = self.post_synth_sdc
        if post_synth_sdc is not None:
            sdc_files.append(post_synth_sdc)

        # TODO: add floorplanning SDC
        if len(sdc_files) > 0:
            sdc_files_arg = "-sdc_files [list {sdc_files}]".format(
                sdc_files=" ".join(sdc_files)
            )
        else:
            blank_sdc = os.path.join(self.run_dir, "blank.sdc")
            self.run_executable(["touch", blank_sdc])
            sdc_files_arg = "-sdc_files {{ {} }}".format(blank_sdc)
        append_mmmc("create_constraint_mode -name {name} {sdc_files_arg}".format(
            name=constraint_mode,
            sdc_files_arg=sdc_files_arg
        ))

        corners = self.get_mmmc_corners()  # type: List[MMMCCorner]
        # In parallel, create the delay corners
        if corners:
            setup_corner = corners[0]  # type: MMMCCorner
            hold_corner = corners[0]  # type: MMMCCorner
            # TODO(colins): handle more than one corner and do something with extra corners
            for corner in corners:
                if corner.type is MMMCCornerType.Setup:
                    setup_corner = corner
                if corner.type is MMMCCornerType.Hold:
                    hold_corner = corner

            # First, create Innovus library sets
            append_mmmc("create_library_set -name {name} -timing [list {list}]".format(
                name="{n}.setup_set".format(n=setup_corner.name),
                list=self.get_timing_libs(setup_corner)
            ))
            append_mmmc("create_library_set -name {name} -timing [list {list}]".format(
                name="{n}.hold_set".format(n=hold_corner.name),
                list=self.get_timing_libs(hold_corner)
            ))
            # Skip opconds for now
            # Next, create Innovus timing conditions
            append_mmmc("create_timing_condition -name {name} -library_sets [list {list}]".format(
                name="{n}.setup_cond".format(n=setup_corner.name),
                list="{n}.setup_set".format(n=setup_corner.name)
            ))
            append_mmmc("create_timing_condition -name {name} -library_sets [list {list}]".format(
                name="{n}.hold_cond".format(n=hold_corner.name),
                list="{n}.hold_set".format(n=hold_corner.name)
            ))
            # Next, create Innovus rc corners from qrc tech files
            append_mmmc("create_rc_corner -name {name} -temperature {tempInCelsius} {qrc}".format(
                name="{n}.setup_rc".format(n=setup_corner.name),
                tempInCelsius=str(setup_corner.temp.value),
                qrc="-qrc_tech {}".format(self.get_mmmc_qrc(setup_corner)) if self.get_mmmc_qrc(setup_corner) != '' else ''
            ))
            append_mmmc("create_rc_corner -name {name} -temperature {tempInCelsius} {qrc}".format(
                name="{n}.hold_rc".format(n=hold_corner.name),
                tempInCelsius=str(hold_corner.temp.value),
                qrc="-qrc_tech {}".format(self.get_mmmc_qrc(hold_corner)) if self.get_mmmc_qrc(hold_corner) != '' else ''
            ))
            # Next, create an Innovus delay corner.
            append_mmmc(
                "create_delay_corner -name {name}_delay -timing_condition {name}_cond -rc_corner {name}_rc".format(
                    name="{n}.setup".format(n=setup_corner.name)
                ))
            append_mmmc(
                "create_delay_corner -name {name}_delay -timing_condition {name}_cond -rc_corner {name}_rc".format(
                    name="{n}.hold".format(n=hold_corner.name)
                ))
            # Next, create the analysis views
            append_mmmc("create_analysis_view -name {name}_view -delay_corner {name}_delay -constraint_mode {constraint}".format(
                name="{n}.setup".format(n=setup_corner.name), constraint=constraint_mode))
            append_mmmc("create_analysis_view -name {name}_view -delay_corner {name}_delay -constraint_mode {constraint}".format(
                name="{n}.hold".format(n=hold_corner.name), constraint=constraint_mode))
            # Finally, apply the analysis view.
            append_mmmc("set_analysis_view -setup {{ {setup_view} }} -hold {{ {hold_view} }}".format(
                setup_view="{n}.setup_view".format(n=setup_corner.name),
                hold_view="{n}.hold_view".format(n=hold_corner.name)
            ))
        else:
            # First, create an Innovus library set.
            library_set_name = "my_lib_set"
            append_mmmc("create_library_set -name {name} -timing [list {list}]".format(
                name=library_set_name,
                list=self.get_timing_libs()
            ))
            # Next, create an Innovus timing condition.
            timing_condition_name = "my_timing_condition"
            append_mmmc("create_timing_condition -name {name} -library_sets [list {list}]".format(
                name=timing_condition_name,
                list=library_set_name
            ))
            # extra junk: -opcond ...
            rc_corner_name = "rc_cond"
            append_mmmc("create_rc_corner -name {name} -temperature {tempInCelsius} {qrc}".format(
                name=rc_corner_name,
                tempInCelsius=120,  # TODO: this should come from tech config
                qrc="-qrc_tech {}".format(self.get_qrc_tech()) if self.get_qrc_tech() != '' else ''
            ))
            # Next, create an Innovus delay corner.
            delay_corner_name = "my_delay_corner"
            append_mmmc(
                "create_delay_corner -name {name} -timing_condition {timing_cond} -rc_corner {rc}".format(
                    name=delay_corner_name,
                    timing_cond=timing_condition_name,
                    rc=rc_corner_name
                ))
            # extra junk: -rc_corner my_rc_corner_maybe_worst
            # Next, create an Innovus analysis view.
            analysis_view_name = "my_view"
            append_mmmc("create_analysis_view -name {name} -delay_corner {corner} -constraint_mode {constraint}".format(
                name=analysis_view_name, corner=delay_corner_name, constraint=constraint_mode))
            # Finally, apply the analysis view.
            # TODO: introduce different views of setup/hold and true multi-corner
            append_mmmc("set_analysis_view -setup {{ {setup_view} }} -hold {{ {hold_view} }}".format(
                setup_view=analysis_view_name,
                hold_view=analysis_view_name
            ))

        return "\n".join(mmmc_output)

    def generate_dont_use_commands(self) -> List[str]:
        """
        Generate a list of dont_use commands for Cadence tools.
        """

        def map_cell(in_cell: str) -> str:
            # "*/" is needed for "get_db lib_cells <cell_expression>"
            if in_cell.startswith("*/"):
                mapped_cell = in_cell  # type: str
            else:
                mapped_cell = "*/" + in_cell

            # Check for cell existence first to avoid Genus erroring out.
            get_db_str = "[get_db lib_cells {mapped_cell}]".format(mapped_cell=mapped_cell)
            # Escaped version for puts.
            get_db_str_escaped = get_db_str.replace('[', '\[').replace(']', '\]')
            return """
puts "set_dont_use {get_db_str_escaped}"
if {{ {get_db_str} ne "" }} {{
    set_dont_use {get_db_str}
}} else {{
    puts "WARNING: cell {mapped_cell} was not found for set_dont_use"
}}
            """.format(get_db_str=get_db_str, get_db_str_escaped=get_db_str_escaped, mapped_cell=mapped_cell)

        return list(map(map_cell, self.get_dont_use_list()))

    def generate_power_spec_commands(self) -> List[str]:
        """
        Generate commands to load a power specification for Cadence tools.
        """

        power_spec_type = str(self.get_setting("vlsi.inputs.power_spec_type"))  # type: str
        power_spec_arg = ""  # type: str
        if power_spec_type == "cpf":
            power_spec_arg = "cpf"
        elif power_spec_type == "upf":
            power_spec_arg = "1801"
        else:
            self.logger.error(
                "Invalid power specification type '{tpe}'; only 'cpf' or 'upf' supported".format(tpe=power_spec_type))
            return []

        power_spec_contents = ""  # type: str
        power_spec_mode = str(self.get_setting("vlsi.inputs.power_spec_mode"))  # type: str
        if power_spec_mode == "empty":
            return []
        elif power_spec_mode == "auto":
            if power_spec_type == "cpf":
                power_spec_contents = self.cpf_power_specification
            elif power_spec_type == "upf":
                power_spec_contents = self.upf_power_specification
        elif power_spec_mode == "manual":
            power_spec_contents = str(self.get_setting("vlsi.inputs.power_spec_contents"))
        else:
            self.logger.error("Invalid power specification mode '{mode}'; using 'empty'.".format(mode=power_spec_mode))
            return []

        # Write the power spec contents to file and include it
        power_spec_file = os.path.join(self.run_dir, "power_spec.{tpe}".format(tpe=power_spec_type))
        with open(power_spec_file, "w") as f:
            f.write(power_spec_contents)
        return ["read_power_intent -{arg} {path}".format(arg=power_spec_arg, path=power_spec_file),
                "commit_power_intent"]

    def write_regs_tcl(self) -> str:
        return '''
        set write_cells_ir "./find_regs_cells.json"
        set write_cells_ir [open $write_cells_ir "w"]
        puts $write_cells_ir "\["

        set refs [get_db [get_db lib_cells -if .is_flop==true] .base_name]

        set len [llength $refs]

        for {set i 0} {$i < [llength $refs]} {incr i} {
            if {$i == $len - 1} {
                puts $write_cells_ir "    \\"[lindex $refs $i]\\""
            } else {
                puts $write_cells_ir "    \\"[lindex $refs $i]\\","
            }
        }

        puts $write_cells_ir "\]"
        close $write_cells_ir
        set write_regs_ir "./find_regs_paths.json"
        set write_regs_ir [open $write_regs_ir "w"]
        puts $write_regs_ir "\["

        set regs [get_db [get_db [all_registers -edge_triggered -output_pins] -if .direction==out] .name]

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

        puts $write_regs_ir "\]"

        close $write_regs_ir
        '''

    def process_reg_paths(self, path: str) -> bool:
        # Post-process the all_regs list here to avoid having too much logic in TCL
        with open(path, "r+") as f:
            reg_paths = json.load(f)
            assert isinstance(reg_paths, List[str]), "Output find_regs_paths.json should be a json list of strings"
            for i in range(len(reg_paths)):
                split = reg_paths[i].split("/")
                if split[-2][-1] == "]":
                    split[-2] = "\\" + split[-2]
                    reg_paths[i] = {"path" : '/'.join(split[0:len(split)-1]), "pin" : split[-1]}
                else:
                    reg_paths[i] = {"path" : '/'.join(split[0:len(split)-1]), "pin" : split[-1]}
            f.seek(0) # Move to beginning to rewrite file
            json.dump(reg_paths, f, indent=2) # Elide the truncation because we are always increasing file size
        return True
