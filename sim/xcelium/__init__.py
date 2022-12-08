#!/usr/bin/env python3

#  HAMMER-VLSI PLUGIN, XCELIUM 

import os
import re
import shutil
import json
import datetime
from typing import Dict, List, Optional, Callable, Tuple
from multiprocessing import Process

import hammer_utils
import hammer_tech

from hammer_tech import HammerTechnologyUtils
from hammer_vlsi import FlowLevel, TimeValue
from hammer_vlsi import HammerSimTool, HammerToolStep, HammerLSFSubmitCommand, HammerLSFSettings
from hammer_logging import HammerVLSILogging
import hammer_tech

import sys
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
    return self.make_steps_from_methods([self.run_xcelium])
  
  def tool_config_prefix(self) -> str:
    return "sim.xcelium"
  
  def post_synth_sdc(self) -> Optional[str]:
    pass

  def generate_arg_file(self, inputs: List[str]=[], options: List[str]=[]) -> None:
    arg_path = self.run_dir+"/xrun.args"
    now = datetime.datetime.now()
    
    if(os.path.exists(arg_path)):
      os.remove(arg_path)
      
    f = open(arg_path,"x")
    f.write("#"+"="*39+"\n")
    f.write("# HAMMER-GENERATED ARGUMENT FILE \n")
    f.write(f"# CREATED AT {now} \n")
    f.write("#"+"="*39+"\n")
    
    f.write("# XRUN INPUT FILES: \n")
    for elem in inputs:
      f.write(elem + "\n") 
    
    f.write("\n# XRUN OPTIONS: \n")
    for elem in options:
      f.write(elem + "\n") 

    f.close()  
    
    return arg_path  
  
  def run_xcelium(self) -> bool:
    
    xcelium_bin = self.get_setting("sim.xcelium.xcelium_bin")
    if not os.path.isfile(xcelium_bin):
      self.logger.error(f"Xcelium (xrun) binary not found at {xcelium_bin}.")
      return False
    
    if not self.check_input_files(self.xcelium_ext):
      return False

    # Extract settings
    tb_name             = self.get_setting("sim.inputs.tb_name")    
    timescale           = self.get_setting("sim.inputs.timescale")
    enhanced_recompile  = self.get_setting("sim.xcelium.enhanced_recompile")
    xmlibdirname        = self.get_setting("sim.xcelium.xmlibdirname")    
    xmlibdirpath        = self.get_setting("sim.xcelium.xmlibdirpath")    
    abspath_input_files = list(map(lambda name: os.path.join(os.getcwd(), name), self.input_files))  

    
    # Generate run options
    options = []
    options.append(f"-top {tb_name}")
    
    if timescale is not None:
      options.append(f"-timescale {timescale}")
    if enhanced_recompile is True:
      options.append("-fast_recompilation")
    if xmlibdirname is not None:
      options.append("xmlibdirname {xmlibdirname}")
    if xmlibdirpath is not None:
      options.append("xmlibdirpath {xmlibdirpath}")

    # Create combined arg file
    arg_file = self.generate_arg_file(abspath_input_files, options)
    
    # Execute
    args = []
    args.append(xcelium_bin)
    args.extend(["-f", arg_file]) 

    # Execute
    self.run_executable(args, cwd=self.run_dir)

    HammerVLSILogging.enable_colour = True
    HammerVLSILogging.enable_tag = True
    return True
  
tool = xcelium
