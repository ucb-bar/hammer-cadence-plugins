#!/usr/bin/env python3

# HAMMER-VLSI PLUGIN, XCELIUM 
# Notes: This plugin sets up xrun to execute in a three-step xrun invocation.
#        This bridges multi-tool direct-invocation and the xrun single-invocation.

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
  def xcelium_ext(self) -> List[str]:
    verilog_ext  = [".v", ".V", ".VS", ".vp", ".VP"]
    sverilog_ext = [".sv",".SV",".svp",".SVP",".svi",".svh",".vlib",".VLIB"]
    c_cxx_ext    = [".c",".cc",".cpp"]
    gz_ext       = [ext + ".gz" for ext in verilog_ext + sverilog_ext]
    z_ext        = [ext + ".z" for ext  in verilog_ext + sverilog_ext]
    return (verilog_ext + sverilog_ext + c_cxx_ext + gz_ext + z_ext)

  @property
  def steps(self) -> List[HammerToolStep]:
    return self.make_steps_from_methods([self.compile_xrun,
                                         self.elaborate_xrun,
                                         self.sim_xrun])
    
  @property
  def tool_config_prefix(self) -> str:
    return "sim.xcelium"
  
  @property
  def sim_input_prefix(self) -> str:
    return "sim.inputs"
  
  @property
  def sim_waveform_prefix(self) -> str:
    return "sim.waveform"
  
  @property
  def xcelium_bin(self) -> str:
    return self.get_setting("sim.xcelium.xcelium_bin")

  @property
  def sim_tcl_file(self) -> str: 
    return os.path.join(self.run_dir, "xrun_sim.tcl")

  def post_synth_sdc(self) -> Optional[str]:
    pass

  def write_gl_files(self) -> bool:
    return True
  
  def get_verilog_models(self) -> List[str]:
    verilog_sim_files = self.technology.read_libs([
        hammer_tech.filters.verilog_sim_filter], 
        hammer_tech.HammerTechnologyUtils.to_plain_item)
    return verilog_sim_files
        
  def fill_outputs(self) -> bool:
    self.output_waveforms = []
    self.output_saifs = []
    self.output_top_module = self.top_module
    self.output_tb_name = self.get_setting(f"{self.sim_input_prefix}.tb_name")
    self.output_tb_dut = self.get_setting(f"{self.sim_input_prefix}.tb_dut")
    self.output_level = self.get_setting(f"{self.sim_input_prefix}.level")
    return True
   
  # Several extract functions are used to process keys in one location. 
  # Processes keys into cmd line options, but also returns a raw input dictionary. 

  # Process xrun options 
  def extract_xrun_opts(self) -> Dict[str, str]:
    xrun_opts_def = [("enhanced_recompile", True),
                     ("xmlibdirname", None),
                     ("xmlibdirpath", None),
                     ("simtmp", None),
                     ("snapshot", None),
                     ("global_access", False)]

    xrun_opts = {opt[0] : self.get_setting(f"{self.tool_config_prefix}.{opt[0]}", opt[1]) for opt in xrun_opts_def}
    xrun_opts_proc = xrun_opts.copy()
    bool_list = ["global_access", "enhanced_recompile"]
    
    if xrun_opts_proc ["global_access"]: 
      xrun_opts_proc ["global_access"] = "+access+rcw"
    else:
      xrun_opts_proc ["global_access"] = ""
    if xrun_opts_proc ["enhanced_recompile"]: xrun_opts_proc ["enhanced_recompile"] = "-fast_recompilation"
    for opt, setting in xrun_opts_proc.items():
      if opt not in bool_list and setting is not None:
        xrun_opts_proc [opt] = f"-{opt} {setting}"
    
    return xrun_opts_proc, xrun_opts
  
  # Process sim options
  def extract_sim_opts(self) -> Tuple[Dict[str, str], Dict[str, str]]:
    abspath_input_files = list(map(lambda name: os.path.join(os.getcwd(), name), self.input_files))
    sim_opts_def =  [("tb_name", None),
                     ("tb_dut", None),
                     ("timescale", None),
                     ("defines", None),
                     ("incdir", None),
                     ("gl_register_force_value", 0)]

    sim_opts = {opt[0] : self.get_setting(f"{self.sim_input_prefix}.{opt[0]}", opt[1]) for opt in sim_opts_def}
    sim_opts_proc = sim_opts.copy()
    sim_opts_proc ["input_files"] =  "\n".join([input for input in abspath_input_files])
    sim_opts_proc ["tb_name"]   = "-top " + sim_opts_proc ["tb_name"]
    sim_opts_proc ["timescale"] = "-timescale " + sim_opts_proc ["timescale"]
    if sim_opts_proc ["defines"] is not None: sim_opts_proc ["defines"] = "\n".join(["-define " + define for define in sim_opts_proc ["defines"]]) 
    if sim_opts_proc ["incdir"] is not None:  sim_opts_proc ["incdir"]  = "\n".join(["-incdir " + incdir for incdir in sim_opts_proc ["incdir"]]) 

    return sim_opts_proc, sim_opts

  # Process waveform options 
  def extract_waveform_opts(self) -> Dict[str, str]:
    wav_opts_def = [("type", None),
                    ("dump_name", "waveform"),
                    ("compression", False),
                    ("shm_incr", "5G"),
                    ("probe_paths", None),
                    ("tcl_opts", None)]

    wav_opts = {opt[0] : self.get_setting(f"{self.sim_waveform_prefix}.{opt[0]}", opt[1]) for opt in wav_opts_def}
    wav_opts_proc = wav_opts.copy()
    wav_opts_proc ["compression"] = "-compress" if wav_opts ["compression"] else ""
    if wav_opts_proc ["probe_paths"] is not None: wav_opts_proc ["probe_paths"] = "\n".join(["probe -create " + path for path in wav_opts_proc ["probe_paths"]]) 
    if wav_opts_proc ["tcl_opts"] is not None:    wav_opts_proc ["tcl_opts"]    = "\n".join(opt for opt in wav_opts_proc ["tcl_opts"]) 

    return wav_opts_proc, wav_opts

  # Label generated files
  def write_header(self, header: str, wrapper: io.TextIOWrapper)->None:
    now = datetime.datetime.now()
    wrapper.write("# "+"="*39+"\n")
    wrapper.write("# "+header+"\n")
    wrapper.write(f"# CREATED AT {now} \n")
    wrapper.write("# "+"="*39+"\n")

  # Create an xrun.arg file
  def generate_arg_file(self, 
                        file_name: str, 
                        header: str, 
                        additional_opt: List[Tuple[str, List[str]]] = [],
                        sim_opt_removal: List[str]=[],
                        xrun_opt_removal: List[str]=[]) -> str:

    # Xrun opts and sim opts must generally be carried through for 1:1:1 correspondence between calls.
    # However, certain opts must be removed (e.g., during sim step)
    xrun_opts_proc = self.extract_xrun_opts()[0]
    sim_opts_proc  = self.extract_sim_opts()[0]
    sim_opt_removal.extend(["gl_register_force_value", "tb_dut"]) 
    [xrun_opts_proc.pop(opt, None) for opt in xrun_opt_removal]
    [sim_opts_proc.pop(opt, None) for opt in sim_opt_removal]
    
    arg_path  = self.run_dir+f"/{file_name}"
    f = open(arg_path,"w+")
    self.write_header(header, f)    
    
    f.write("\n# XRUN OPTIONS: \n")
    [f.write(elem + "\n") for elem in xrun_opts_proc.values() if elem is not None]
    f.write("\n# SIM OPTIONS: \n")
    [f.write(elem + "\n") for elem in sim_opts_proc.values() if elem is not None]
    for opt_list in additional_opt: 
      if opt_list[1]: 
        f.write(f"\n# {opt_list[0]} OPTIONS: \n")
        [f.write(elem + "\n") for elem in opt_list[1]]
    f.close()  
    
    return arg_path  
  
  # Deposit values
  # Try to maintain some parity with vcs plugin.
  def generate_gl_deposit_tcl(self) -> List[str]:
    sim_opts_proc, sim_opts  = self.extract_sim_opts() 
    tb_prefix = sim_opts["tb_name"] + '.' + sim_opts["tb_dut"]
    force_val = sim_opts["gl_register_force_value"]
    
    abspath_all_regs = os.path.join(os.getcwd(), self.all_regs)
    if not os.path.isfile(abspath_all_regs):
      self.logger.error("List of all regs json not found as expected at {0}".format(self.all_regs))

    formatted_deposit = []
    with open(abspath_all_regs) as reg_file:
      reg_json = json.load(reg_file)
      assert isinstance(reg_json, List), "list of all sequential cells should be a json list of dictionaries from string to string not {}".format(type(reg_json))
      for reg in sorted(reg_json, key=lambda r: len(r["path"])): # TODO: This is a workaround for a bug in P-2019.06
        path = reg["path"]
        path = path.split('/')
        special_char =['[',']','#','$',';','!',"{",'}','\\']
        path = ['@{' + subpath + ' }' if any(char in subpath for char in special_char) else subpath for subpath in path]
        path='.'.join(path)
        pin = reg["pin"]
        formatted_deposit.append("deposit " + tb_prefix + "." + path + "." + pin + " = " + str(force_val) + " -release")
        
    return formatted_deposit

  # Creates a tcl driver for sim step.
  def generate_sim_tcl(self) -> bool:
    xmsimrc_def = self.get_setting("sim.xcelium.xmsimrc_def")
    wav_opts_proc, wav_opts = self.extract_waveform_opts()

    f = open(self.sim_tcl_file,"w+")
    self.write_header("HAMMER-GEN SIM TCL DRIVER", f)    
    f.write(f"source {xmsimrc_def} \n")
    
    if wav_opts["type"] is not None:
      if wav_opts["type"]   == "VCD":  f.write(f'database -open -vcd vcddb -into {wav_opts["dump_name"]}.vcd -default {wav_opts_proc["compression"]} \n')
      elif wav_opts["type"] == "EVCD": f.write(f'database -open -evcd evcddb -into {wav_opts["dump_name"]}.evcd -default {wav_opts_proc["compression"]} \n')
      elif wav_opts["type"] == "SHM":  f.write(f'database -open -shm shmdb -into {wav_opts["dump_name"]}.shm -event -default {wav_opts_proc["compression"]} {wav_opts_proc["shm_incr"]} \n')
      if wav_opts_proc["probe_paths"] is not None: 
        [f.write(f'{wav_opts_proc["probe_paths"]}\n')]
      if wav_opts_proc["tcl_opts"] is not None: [f.write(f'{wav_opts_proc["tcl_opts"]}\n')]
    
    if self.level.is_gatelevel(): 
      formatted_deposit = self.generate_gl_deposit_tcl()
      [f.write(f'{deposit}\n') for deposit in formatted_deposit]

    f.write("run \n")
    f.write("database -close *db \n")
    f.write("exit")
    f.close()  

    return True

  def compile_xrun(self) -> bool:
    
    if not os.path.isfile(self.xcelium_bin):
      self.logger.error(f"Xcelium (xrun) binary not found at {self.xcelium_bin}.")
      return False
  
    if not self.check_input_files(self.xcelium_ext):
      return False

    # Gather complation-only options
    compile_opts  = self.get_setting(f"{self.tool_config_prefix}.compile_opts", [])       
    compile_opts.append("-logfile xrun_compile.log")
    compile_opts = ('COMPILE', compile_opts)
    
    arg_file_path = self.generate_arg_file("xrun_compile.arg", "HAMMER-GEN XRUN COMPILE ARG FILE", [compile_opts])
    args =[self.xcelium_bin]
    args.append(f"-compile -f {arg_file_path}")
    
    self.run_executable(args, cwd=self.run_dir)
    HammerVLSILogging.enable_colour = True
    HammerVLSILogging.enable_tag = True
    return True
  
  def elaborate_xrun(self) -> bool: 
    # Gather elaboration-only options
    elab_opts = self.get_setting(f"{self.tool_config_prefix}.elab_opts", [])
    elab_opts.append("-logfile xrun_elab.log")
    elab_opts.append("-glsperf")
    elab_opts.append("-genafile access.txt")    
    if self.level.is_gatelevel(): elab_opts.extend(self.get_verilog_models())    
    elab_opts = ('ELABORATION', elab_opts)
    
    arg_file_path = self.generate_arg_file("xrun_elab.arg", "HAMMER-GEN XRUN ELAB ARG FILE", [elab_opts])
    args =[self.xcelium_bin]
    args.append(f"-elaborate -f {arg_file_path}")
    
    self.run_executable(args, cwd=self.run_dir)
    return True

  def sim_xrun(self) -> bool:
    sim_opts = self.get_setting(f"{self.sim_input_prefix}.options", [])
    sim_opts_removal  = ["tb_name", "input_files", "incdir"]
    xrun_opts_removal = ["enhanced_recompile"]
    sim_opts = ('SIMULATION', sim_opts) 
    
    arg_file_path = self.generate_arg_file("xrun_sim.arg", "HAMMER-GEN XRUN SIM ARG FILE", [sim_opts],
                                           sim_opt_removal = sim_opts_removal,
                                           xrun_opt_removal = xrun_opts_removal)    
    args =[self.xcelium_bin]
    args.append(f"-R -f {arg_file_path} -input {self.sim_tcl_file}")

    self.generate_sim_tcl() 
    self.run_executable(args, cwd=self.run_dir)
    return True

tool = xcelium
