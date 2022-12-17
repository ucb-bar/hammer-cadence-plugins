#!/usr/bin/env python3

# HAMMER-VLSI PLUGIN, XCELIUM 

import os
import re
import shutil
import json
import datetime
import io
import sys
from typing import Dict, List, Optional, Callable, Tuple
from multiprocessing import Process

import hammer_utils
import hammer_tech
from hammer_tech import HammerTechnologyUtils
from hammer_vlsi import FlowLevel, TimeValue
from hammer_vlsi import HammerSimTool, HammerToolStep, HammerLSFSubmitCommand, HammerLSFSettings
from hammer_logging import HammerVLSILogging

sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)),"../../common"))
from tool import CadenceTool
class xcelium(HammerSimTool, CadenceTool):

  @property
  def access_tab_file_path(self) -> str:
    return os.path.join(self.run_dir, "access.tab")

  @property
  def steps(self) -> List[HammerToolStep]:
    return self.make_steps_from_methods([self.write_gl_files,
                                         self.run_xcelium])  
    
  @property
  def xcelium_ext(self) -> List[str]:
    verilog_ext  = [".v", ".V", ".VS", ".vp", ".VP"]
    sverilog_ext = [".sv",".SV",".svp",".SVP",".svi",".svh",".vlib",".VLIB"]
    c_cxx_ext    = [".c",".cc",".cpp"]
    gz_ext       = [ext + ".gz" for ext in verilog_ext + sverilog_ext]
    z_ext        = [ext + ".z" for ext  in verilog_ext + sverilog_ext]
    return (verilog_ext + sverilog_ext + c_cxx_ext + gz_ext + z_ext)

  def tool_config_prefix(self) -> str:
    return "sim.xcelium"
  
  def post_synth_sdc(self) -> Optional[str]:
    pass

  def write_gl_files(self) -> bool:
    if self.level == FlowLevel.RTL:
        return True

    tb_prefix = self.get_setting("sim.inputs.tb_name") + '.' + self.get_setting("sim.inputs.tb_dut")
    force_val = self.get_setting("sim.inputs.gl_register_force_value")
    abspath_seq_cells = os.path.join(os.getcwd(), self.seq_cells)
   
    if not os.path.isfile(abspath_seq_cells):
      self.logger.error(f"List of seq cells json not found as expected at {self.seq_cells}")

    with open(self.access_tab_file_path, "w") as f:
      with open(abspath_seq_cells) as seq_file:
                seq_json = json.load(seq_file)
                assert isinstance(seq_json, List), "List of all sequential cells should be a json list of strings not {}".format(type(seq_json))
                for cell in seq_json:
                    f.write("acc=wn:{cell_name}\n".format(cell_name=cell))

    abspath_all_regs = os.path.join(os.getcwd(), self.all_regs)
    if not os.path.isfile(abspath_all_regs):
      self.logger.error("List of all regs json not found as expected at {0}".format(self.all_regs))

    with open(self.force_regs_file_path, "w") as f:
      with open(abspath_all_regs) as reg_file:
          reg_json = json.load(reg_file)
          assert isinstance(reg_json, List), "list of all sequential cells should be a json list of dictionaries from string to string not {}".format(type(reg_json))
          for reg in sorted(reg_json, key=lambda r: len(r["path"])): # TODO: This is a workaround for a bug in P-2019.06
            special_char =['[',']','#','$',';','!',"{",'}','\\']

            path = reg["path"]
            path = path.split('/')
            path = [subpath.removesuffix('\\') for subpath in path]
            path = ['@_{' + subpath + ' }' if any(char in subpath for char in special_char) else subpath for subpath in path]
            path='.'.join(path)

            pin = reg["pin"]
            f.write("deposit " + tb_prefix + "." + path + "." + pin + " = " + str(force_val) + "\n")

    return True

  def fill_outputs(self) -> bool:
      self.output_waveforms = []
      self.output_saifs = []
      self.output_top_module = self.top_module
      self.output_tb_name = self.get_setting("sim.inputs.tb_name")
      self.output_tb_dut = self.get_setting("sim.inputs.tb_dut")
      self.output_level = self.get_setting("sim.inputs.level")
      return True

  # Label generated files
  def write_header(self, header: str, wrapper: io.TextIOWrapper)->None:
    now = datetime.datetime.now()
    wrapper.write("# "+"="*39+"\n")
    wrapper.write("# "+header+"\n")
    wrapper.write(f"# CREATED AT {now} \n")
    wrapper.write("# "+"="*39+"\n")

  # Create a combined argument file to collect xrun inputs, xrun options
  def generate_arg_file(self, run_inputs: List[str]=[], run_directives: List[str]=[], run_additional_opts: List[str]=[]) -> str:
    arg_path = self.run_dir+"/xrun.args"
    f = open(arg_path,"w+")
    self.write_header("HAMMER-GENERATED ARGUMENT SCRIPT", f)    
    f.write("# XRUN INPUT FILES: \n")
    [f.write(elem + "\n") for elem in run_inputs]
    f.write("\n# XRUN PRIMARY DIRECTIVES: \n")
    [f.write(elem + "\n") for elem in run_directives]
    f.write("\n# XRUN ADDITIONAL OPTIONS: \n")
    [f.write(elem + "\n") for elem in run_additional_opts]
    f.close()  
    
    return arg_path  

  # Creates a tcl driver
  def generate_sim_tcl(self) -> str:
    xmsimrc_def = self.get_setting("sim.xcelium.xmsimrc_def")
    tcl_path    = self.run_dir+"/xrun.tcl"
    tcl_mode    = self.get_setting("sim.xcelium.tcl_mode")
    
    f = open(tcl_path,"w+")
    self.write_header("HAMMER-GENERATED TCL SCRIPT", f)    
    f.write(f"source {xmsimrc_def} \n")
    
    if tcl_mode:
      # Waveform dumping
      signal_paths = self.get_setting("sim.waveform.signal_paths")
      signal_opts  = self.get_setting("sim.waveform.signal_opts")
      dump_type    = self.get_setting("sim.waveform.type")
      dump_name    = self.get_setting("sim.waveform.dump_name", "waveform")
      dump_compression = "-compress" if self.get_setting("sim.waveform.compression") else ""
      shm_incr  = "-incsize 5G" if self.get_setting("sim.waveform.shm_incr") else ""

      if dump_type == "VCD":
        f.write(f"database -open -vcd vcddb -into {dump_name}.vcd -default {dump_compression} \n")
      if dump_type == "EVCD":
        f.write(f"database -open -evcd evcddb -into {dump_name}.evcd -default {dump_compression} \n")
      if dump_type == "SHM":
        f.write(f"database -open -shm shmdb -into {dump_name}.shm -event -default {dump_compression} {shm_incr} \n")
      #f.write("probe -create -all -depth all \n")
      if signal_paths is not None:
        [f.write(f"probe -create {signal} \n") for signal in signal_paths]
      if signal_opts is not None:
        [f.write(f"{signal_opts} \n") for opt in signal_opts]

    f.write("run \n")
    f.write("database -close *db \n")
    f.write("exit")
    f.close()  

    return tcl_path

  def generate_scripts(self) -> None:
    gen_folder_path = self.run_dir+"/generated-scripts"
    os.makedirs(gen_folder_path, exist_ok=True)
    self.generate_open_sh(gen_folder_path)

  def generate_open_sh(self, gen_folder_path: str) -> None:
    shell_script_path = gen_folder_path + "/open_db.sh"
    tcl_script_path   = self.generate_open_tcl(gen_folder_path)
        
    f = open(shell_script_path,"w+")
    self.write_header("HAMMER-GENERATED BASH SCRIPT", f)    
    f.write("#!/bin/bash \n")
    f.write(f"xrun -input {tcl_script_path}")

  def generate_open_tcl(self, gen_folder_path: str) -> str: 
    script_path = gen_folder_path + "/open_db_xcelium.tcl"

    f = open(script_path,"w+")
    self.write_header("HAMMER-GENERATED TCL SCRIPT", f)    
    return script_path

  def run_xcelium(self) -> bool:
    
    xcelium_bin = self.get_setting("sim.xcelium.xcelium_bin")
    if not os.path.isfile(xcelium_bin):
      self.logger.error(f"Xcelium (xrun) binary not found at {xcelium_bin}.")
      return False
    
    if not self.check_input_files(self.xcelium_ext):
      return False

    # xrun customization options (xrun-specific, non-sim related) 
    enhanced_recompile  = self.get_setting("sim.xcelium.enhanced_recompile")
    xmlibdirname        = self.get_setting("sim.xcelium.xmlibdirname")    
    xmlibdirpath        = self.get_setting("sim.xcelium.xmlibdirpath")    
    simtmp              = self.get_setting("sim.xcelium.simtmp")    
    snapshot            = self.get_setting("sim.xcelium.snapshot")  
    global_access       = self.get_setting("sim.xcelium.global_access") 
    # Important sim-related options
    tb_name             = self.get_setting("sim.inputs.tb_name")    
    timescale           = self.get_setting("sim.inputs.timescale")
    sim_options         = self.get_setting("sim.inputs.options", [])
    sim_defines         = self.get_setting("sim.inputs.defines", [])
    sim_incdirs         = self.get_setting("sim.inputs.incdir", [])

    # Assemble run command
    run_inputs = list(map(lambda name: os.path.join(os.getcwd(), name), self.input_files))  
    run_directives = []
    run_directives.append(f"-top {tb_name}")

    if global_access is True:
      run_directives.append(f"+access+rcw")
    for define in sim_defines:
      run_directives.extend(['-define ' + define])
    for incdir in sim_incdirs:
      run_directives.extend(['-incdir ' + incdir])      
    if timescale is not None:
      run_directives.append(f"-timescale {timescale}")
    if enhanced_recompile is True:
      run_directives.append("-fast_recompilation")
    if xmlibdirname is not None:
      run_directives.append(f"-xmlibdirname {xmlibdirname}")
    if xmlibdirpath is not None:
      run_directives.append(f"-xmlibdirpath {xmlibdirpath}")
    if simtmp is not None:
      run_directives.append(f"-simtmp {simtmp}")
    if snapshot is not None:
      run_directives.append(f"-snapshot {snapshot}")
    
    tcl_file = self.generate_sim_tcl()
    run_directives.append(f"-input {tcl_file}") 
    
    # Create combined arg file
    arg_file = self.generate_arg_file(run_inputs, run_directives, sim_options)
    
    # Execute command
    args = []
    args.append(xcelium_bin)
    args.extend(["-f", arg_file]) 
    self.run_executable(args, cwd=self.run_dir)

    # Create generated scripts
    self.generate_scripts()

    HammerVLSILogging.enable_colour = True
    HammerVLSILogging.enable_tag = True
    return True
  
tool = xcelium
