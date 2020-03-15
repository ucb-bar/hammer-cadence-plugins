#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  hammer-vlsi plugin for Cadence Xcelium
#
#  See LICENSE for license details.

from hammer_vlsi import HammerSimTool, HammerToolStep, TCLTool
from hammer_vlsi import CadenceTool
from hammer_logging import HammerVLSILogging

from typing import Dict, List, Optional, Callable, Tuple

from hammer_vlsi import SimulationLevel

import hammer_utils
import hammer_tech
from hammer_tech import HammerTechnologyUtils

import os
import re
import shutil

################### VALID KEYS #############################
# input_files
# top_module
# options
# defines
# compiler_opts
# timescale
# tb_dut
# sdf_file
# gl_register_force_value (0 or 1. Forces all registers to be initialized!)
#
# timing_annotated (true or false)
# execution_flags
# execute_sim (true or false) -elaborate option will accomplish this
# level
# all_regs
# seq_cells
############################################################

# TODO compiler flags for c/cpp files
# TODO benchmark tests
# TODO force_regs function
# TODO verify gate level works
# TODO verify timing annotated
# TODO access tab stuff
# TODO revisit dependency issue (find a way to not run sim if snapshot was not re-generated)
# TODO tests?


class Xcelium(HammerSimTool, CadenceTool, TCLTool):
    def post_synth_sdc(self) -> Optional[str]:
        pass

    def tool_config_prefix(self) -> str:
        return "sim.xcelium"

    @property
    def steps(self) -> List[HammerToolStep]:
        return self.make_steps_from_methods(
            [self.write_gl_files, self.run_xrun, self.run_simulation])

    def benchmark_run_dir(self, bmark_path: str) -> str:
        """Generate a benchmark run directory."""
        # TODO(ucb-bar/hammer#462) this method should be passed the name of the bmark rather than its path
        bmark = os.path.basename(bmark_path)
        return os.path.join(self.run_dir, bmark)

    @property
    def force_regs_file_path(self) -> str:
        return os.path.join(self.run_dir, "force_regs.tcl")

    @property
    def access_tab_file_path(self) -> str:
        return os.path.join(self.run_dir, "access.tab")

    @property
    def simulator_snapshot_name(self) -> str:
        return "xrun_snapshot"

    @property
    def simulator_work_dir(self) -> str:
        return os.path.join(self.run_dir, "xcelium.d")

    @property
    def simulator_executable_path(self) -> str:
        return os.path.join(self.run_dir, self.simulator_work_dir,
                            self.simulator_snapshot_name + '.d')

    @property
    def run_tcl_path(self) -> str:
        return os.path.join(self.run_dir, "run.tcl")

    @property
    def env_vars(self) -> Dict[str, str]:
        v = dict(super().env_vars)
        v["XCELIUM_HOME"] = self.get_setting("sim.xcelium.xcelium_home")
        return v

    def get_verilog_models(self) -> List[str]:
        verilog_sim_files = self.technology.read_libs(
            [hammer_tech.filters.verilog_sim_filter],
            hammer_tech.HammerTechnologyUtils.to_plain_item)
        return verilog_sim_files

    def write_gl_files(self) -> bool:
        if self.level == SimulationLevel.RTL:
            return True

        tb_prefix = self.get_setting("sim.inputs.tb_dut")
        force_val = self.get_setting("sim.inputs.gl_register_force_value")

        seq_cells = self.seq_cells
        with open(self.access_tab_file_path, "w") as f:
            for cell in seq_cells:
                f.write("acc=wn:{cell_name}\n".format(cell_name=cell))

        all_regs = self.all_regs

        with open(self.force_regs_file_path, "w") as f:
            for reg in all_regs:
                path = reg["path"]
                path = '.'.join(path.split('/'))
                pin = reg["pin"]
                f.write("force " + tb_prefix + "." + path + " ." + pin + " " +
                        str(force_val) + "\n")

        return True

    def run_xrun(self) -> bool:
        # run through inputs and append to CL arguments
        xrun_bin = self.get_setting("sim.xcelium.xcelium_bin")
        if not os.path.isfile(xrun_bin):
            self.logger.error(
                "Xcelium (xrun) binary not found as expected at {0}".format(
                    xrun_bin))
            return False

        if not self.check_input_files([".v", ".sv", ".so", ".cc", ".c"]):
            return False

        top_module = self.top_module
        ###################################################################
        #compiler_opts = self.get_setting("sim.inputs.compiler_opts", [])
        ###################################################################

        # TODO(johnwright) sanity check the timescale string
        timescale = self.get_setting("sim.inputs.timescale")
        input_files = list(self.input_files)
        options = self.get_setting("sim.inputs.options", [])
        defines = self.get_setting("sim.inputs.defines", [])
        ###################################################################
        #access_tab_filename = self.access_tab_file_path
        ###################################################################
        tb_name = self.get_setting("sim.inputs.tb_name")

        # Build args
        args = [xrun_bin, "-64bit"]

        if timescale is not None:
            args.append('-timescale {}'.format(timescale))

        ###################################################################
        ## Add in options we pass to the C++ compiler
        #args.extend(['-CC', '-I$(VCS_HOME)/include'])
        #for compiler_opt in compiler_opts:
        #    args.extend(['-CC', compiler_opt])
        ###################################################################

        # black box options
        args.extend(options)

        # Add in all input files
        args.extend(input_files)

        # Note: we always want to get the verilog models because most real designs will instantate a few
        # tech-specific cells in the source RTL (IO cells, clock gaters, etc.)
        args.extend(self.get_verilog_models())

        for define in defines:
            args.extend(['+define+' + define])

        if self.level == SimulationLevel.GateLevel:
            ###################################################################
            #args.extend(['-P'])
            #args.extend([access_tab_filename])
            #args.extend(['-debug'])
            #if self.get_setting("sim.inputs.timing_annotated"):
            #    args.extend(["+neg_tchk"])
            #    args.extend(["+sdfverbose"])
            #    args.extend(["-negdelay"])
            #    args.extend(["-sdf"])
            #    args.extend(["max:{top}:{sdf}".format(run_dir=self.run_dir, top=top_module, sdf=self.sdf_file)])
            #else:
            #    args.extend(["+notimingcheck"])
            #    args.extend(["+delay_mode_zero"])
            ###################################################################
            # Append sourcing of force regs tcl
            self.append("source " + self.force_regs_file_path)

        args.extend(["-top", tb_name])

        args.extend(['-elaborate'])
        args.extend(['-snapshot', self.simulator_snapshot_name])

        HammerVLSILogging.enable_colour = False
        HammerVLSILogging.enable_tag = False

        # Generate a simulator
        self.run_executable(args, cwd=self.run_dir)

        # Create run tcl for simulation step
        self.append("run")
        self.append("exit")
        with open(self.run_tcl_path, "w") as fp:
            fp.write("\n".join(self.output))

        HammerVLSILogging.enable_colour = True
        HammerVLSILogging.enable_tag = True

        return os.path.exists(
            self.simulator_executable_path) and os.path.exists(
                self.run_tcl_path)

    def run_simulation(self) -> bool:
        if not self.get_setting("sim.inputs.execute_sim"):
            self.logger.warning(
                "Not running any simulations because sim.inputs.execute_sim is unset."
            )
            return True

        ###################################################################
        #for benchmark in self.benchmarks:
        #    if not os.path.isfile(benchmark):
        #      self.logger.error("benchmark not found as expected at {0}".format(vcs_bin))
        #      return False
        ###################################################################

        # Setup simulation arguments
        args = [self.get_setting("sim.xcelium.xcelium_bin")]
        args.extend(["-r", self.simulator_snapshot_name])

        # Execution flags
        exec_flags_prepend = self.get_setting(
            "sim.inputs.execution_flags_prepend", [])
        exec_flags = self.get_setting("sim.inputs.execution_flags", [])
        exec_flags_append = self.get_setting(
            "sim.inputs.execution_flags_append", [])
        args.extend(exec_flags_prepend)
        args.extend(exec_flags)
        args.extend(["-input", self.run_tcl_path])
        args.extend(exec_flags_append)

        HammerVLSILogging.enable_colour = False
        HammerVLSILogging.enable_tag = False

        ###################################################################
        # TODO(johnwright) We should optionally parallelize this in the future.
        for benchmark in self.benchmarks:
            bmark_run_dir = self.benchmark_run_dir(benchmark)
            # Make the rundir if it does not exist
            hammer_utils.mkdir_p(bmark_run_dir)
            self.run_executable(args + [benchmark], cwd=bmark_run_dir)
        ##################################################################

        if self.benchmarks == []:
            self.run_executable(args, cwd=self.run_dir)

        HammerVLSILogging.enable_colour = True
        HammerVLSILogging.enable_tag = True

        return True


tool = Xcelium
