# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# https://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or https://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

from ramble.modkit import *  # noqa: F403

class HpcImageValidation(BasicModifier):
    """Define a modifier for a subset of hpc image validators

    The commands here are used to validate the VMs used in the benchmark
    testing of the HPC VM images.  As right now the output is collected
    as a long single line string with newlines replaced with \n literals.
    """
    name = "hpc-image-validation"

    tags('system-info', 'sysinfo', 'platform-info')

    maintainers('carsondunbar')

    mode('standard', description='Tests for hpc-image VM validation')
    default_mode('standard')

    # Commands to be run, keys are simple names for reading, values are the
    # actual commands
    cmds = {"uname" : "uname -a",
            "cmdline": "cat /proc/cmdline",
            "network_irqs": "cat /sys/class/net/eth0/queues/tx-*/xps_cpus",
            "lstopo": "lstopo-no-graphics",
            "tund_adm_log": "tuned-adm active",
            "lsmod": "lsmod",
            "systemctl": "sudo systemctl -al",
            "sysctl": "sudo sysctl -a",
            "interrupts": "cat /proc/interrupts"}

    for k,v in cmds.items():
        variable_modification(f'{k}_log', '{{experiment_run_dir}}/{name}.log'.format(name=k), method='set', modes=['standard'])
        archive_pattern(f'{k}.log')
        figure_of_merit(f'{k}', fom_regex=r'(?P<fom>[\S\s\n]*)', group_name='fom', units='', log_file='{' + k + '_log}')
    
    register_builtin('hpc_image_validation_exec', injection_method='append')

    # figure_of_merit_context('nic_statistics', regex=r'Architecture:\s+(?P<arch>[\w-]+)', output_format='{arch}')

    # figure_of_merit("Current active profile", fom_regex=r'Current active profile:\s+(?P<fom>.*)', group_name='fom', units='', log_file='{tuned_adm_log}')
    
    def hpc_image_validation_exec(self):
        """Create a list of bash commands based on the commands above.
        Each command is run through sed to remove newlines and replace them with
        \n literals
        """    
        return [f'{v}' + r" | sed -E ':a;N;$!ba;s/\r{0,1}\n/\\n/g' >> {" + k + "_log}" for k,v in self.cmds.items()]
