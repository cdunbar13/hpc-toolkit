# Copyright 2024 "Google LLC"
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import io
import sys
import uuid
import tarfile
import argparse
import time
import shutil
import subprocess
from typing import List, Union


# TODO Replace gcloud commands with gcloud pip module commands
RUN_DIR = os.getcwd()
SCRIPT_DIR = sys.path[0]
BUILD_PROJECT_ID = "cloud-hpc-image-devel"
GHPC_DIR = os.path.join(RUN_DIR, "hpc-toolkit")
GHPC_BIN_SRC = os.path.join(GHPC_DIR, "ghpc")
GHPC_BIN_DST = os.path.join("/usr/local/bin", "ghpc")
NET_NAME = "hpc-image-testing-net"
NET_BP = os.path.join(SCRIPT_DIR, "testing-vpc.yaml")
TEMPLATE_BP = os.path.join(SCRIPT_DIR, "intel-mpi-bm-template.yaml")
NET_DEP_FOLDER = "hpc-image-vpc"
DEPLOYMENT_BUCKET="hpc-image-deployments"
ENV_COPY = os.environ.copy()

DESCRIPTION = '''
    hpc-image-test.py is designed to aid in running tests to compare the 
    performance of various images built with HPC in mind.  Users may specify
    an image family and how many of the most recent images to test, or a list
    of comma-delimited image names to test.
    '''

USAGE = '''
    hpc-image-tester.py [-h] -p PROJECT -r RAMBLE_FILE [-f IMAGE_FAMILY] [-n NUM_IMAGES] [-m MACHINE_TYPE] [-i IMAGE_NAMES] [-z ZONE]
'''

destroy_procs: List[subprocess.Popen] = []

def print_divider(msg: str):
    print(f" {msg}".center(80, "*") + "\n")

def print_proc_lines(io: Union[None, io.TextIOWrapper]):
    if io is not None:
        lines = io.readlines()
        for line in lines:
            print(line)

def run_command(cmd: str, cmd_msg: str = None, err_msg: str = None, print_out: bool = False, wait: bool = True) -> Union[subprocess.Popen, str]:
    if print_out:
        if cmd_msg is not None:
            print_divider(f"Start: {cmd_msg}")
        else:
            print_divider("Run Command")
        print(f"Running command: \"{cmd}\"")
    
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1, universal_newlines=True, shell=True)
    if not wait:
        return p
    stdout = ""
    while p.poll() is None:
        for line in p.stdout.readlines():
            if print_out:
                print(line, end='', flush=True)
            stdout += line
        time.sleep(2)
    for line in p.stdout.readlines():
        if print_out:
            print(line, end='')

    if print_out:
        if cmd_msg is not None:
            print_divider(f"End: {cmd_msg}")
        else:
            print_divider("End Command")

    if p.returncode:
        raise subprocess.SubprocessError(f"{err_msg}:\n" + "".join(p.stderr.readlines()))
    return stdout

def install_go(go_ver: str = "1.21.5") -> None:
    if shutil.which("go") is not None:
        return
    print("Golang not found on system, attempting to install precompiled package")
    fn = f"go{go_ver}.linux-amd64.tar.gz"
    abs_fn = os.path.join(RUN_DIR, fn)
    cmd = f"curl -OL https://golang.org/dl/{fn}"
    run_command(cmd, "Error downloading Go package")
    with open(abs_fn, mode="rb") as fp:
        tf = tarfile.open(fileobj=fp)
        tf.extractall(RUN_DIR)
    os.remove(abs_fn)
    ENV_COPY['PATH'] = f"{RUN_DIR}/go/bin:{ENV_COPY['PATH']}"
    print("Golang installed")

def get_latest_n_images(project: str, img_family: str, n: int = 3) -> List[str]:
    cmd = f"gcloud compute images list --project={project} " \
           "--no-standard-images --show-deprecated " \
           f"--filter=\'family={img_family}\' --format=\'value(name)\' " \
           f"--sort-by=~name --limit {n}"
    err_msg = f"Error getting last {n} images from image family {img_family}"
    res = run_command(cmd, err_msg=err_msg)
    if isinstance(res, str):
        imgs = res.rsplit()
        if len(imgs) < n:
            print(f"WARNING: Only {len(imgs)} images were found in family " \
                  f"{img_family}")
        return imgs
    print(f"No images were found for the {img_family} family, exiting")
    test_exit(1)

def get_data(image_name: str, deployment_name: str, output_dir: str) -> None:
    cmd = "gcloud compute scp --recurse --project {project} --zone {zone} " \
          f"{deployment_name}-0:/shared/test_workspace/experiments/intel-mpi-benchmarks/* " \
          f"{output_dir}/{image_name} </dev/null"
    run_command(cmd, "Error downloading Ramble test results")

def send_deployment_to_bucket(deployment_name: str, bucket_name: str) -> None:
    outfile = f"{deployment_name}.tar.gz"
    with tarfile.open(outfile, "w:gz") as tar:
        tar.add(deployment_name)
    cmd = f"gsutil cp {outfile} gs://{bucket_name}"
    run_command(cmd, f"Uploading {deployment_name} folder to GCS bucket", 
                "Error downloading Ramble test results")

def build_ghpc(branch: str = "develop") -> None:
    if shutil.which("ghpc") is not None:
        print("GHPC already in the correct location, not rebuilding")
        return
    if os.path.exists(GHPC_DIR):
        shutil.rmtree(GHPC_DIR)
    # install_go()
    print("Building ghpc")
    run_command(["make"], "Error making ghpc")
    shutil.copy(GHPC_BIN_SRC, GHPC_BIN_DST)
    os.chdir(RUN_DIR)

def build_network() -> None:
    # TODO: Check that we have a subnetwork in the correct zone/region
    cmd = f"gcloud compute networks list --project={BUILD_PROJECT_ID} --format=\'value(name)\' --filter=\'name:{NET_NAME}\'"
    res = run_command(cmd, "Build Network", "Error getting list of gcloud networks")
    if res is None:
        cmd = f"ghpc create -w --vars project_id={BUILD_PROJECT_ID} {NET_BP}"
        run_command(cmd, "Error creating network deployment folder")
        cmd = f"ghpc deploy {NET_DEP_FOLDER} --auto-approve"
        run_command(cmd, "Error deploying network")

def create_deployment(project:str, image_name: str, zone: str, region: str,
                      ramble_file: str, machine_type: str = "c2-standard-60",
                      num_vms: int = 8) -> str:
    # if "BUILD_ID_SHORT" in os.environ:
    #     build_id = os.environ["BUILD_ID_SHORT"][:6]
    # else:
    build_id = uuid.uuid4().hex[:6]
    dep_name=f"{image_name.replace('linux-', '')}-{build_id}"
    dep_vars = [f"project_id={BUILD_PROJECT_ID}",
                f"ramble_config_location={ramble_file}",
                "add_deployment_name_before_prefix=true",
                f"compute_machine_type={machine_type}",
                f"image_project={project}",
                f"image_name={image_name}",
                f"region={region}",
                f"zone={zone}",
                f"deployment_name={dep_name}",
                f"num_instances={num_vms}"]
    cmd = f"ghpc create -w {TEMPLATE_BP} --vars {','.join(dep_vars)}"
    run_command(cmd, f"Create Deployment in zone: {zone}", 
                "Error creating microbenchmark deployment", print_out=True)
    send_deployment_to_bucket(dep_name, DEPLOYMENT_BUCKET)
    return dep_name

def deploy_tests(dep_dir: str, project: str, zone: str, dep_name: str) -> str:
    cmd = f"ghpc deploy {dep_dir} --auto-approve"
    try:
        run_command(cmd, "Deploying Tests", "Error deploying microbenchmark tests", print_out=True)
    except subprocess.SubprocessError as e:
        err_msg = e.__str__()
        if "does not have enough resources" in err_msg:
            print(f"{e}\nDestroying deployment, and trying in a new zone")
            destroy_deployment(dep_dir)
            return "stockout"
        elif "Error waiting for instance to create: Quota" in err_msg:
            print(f"{e}\nDestroying deployment, and trying in a new zone")
            destroy_deployment(dep_dir)
            return "quota"
        else:
            print(f"Error during deployment: \n{e}\n")
            try:
                cmd = f"gcloud compute instances get-serial-port-output {dep_name}-0 --port 1 --zone {zone} --project {BUILD_PROJECT_ID}"
                run_command(cmd, "Getting Serial Output", f"Error printing serial console from {dep_name}-0", print_out=True)
            except subprocess.SubprocessError as e:
                print(e)
            print("Destroying deployment then exiting")
            time.sleep(60)
            destroy_deployment(dep_dir)
            test_exit(1)
    print(f"Test of {dep_dir} has completed")
    return ""

def destroy_deployment(dep_dir: str) -> None:
    cmd = f"ghpc destroy {dep_dir} --auto-approve"
    p = run_command(cmd, "Destroy Deployment", "Error destroying microbenchmark deployment", wait=False)
    destroy_procs.append(p)

def check_procs() -> None:
    i = 0
    while i < len(destroy_procs):
        poll = destroy_procs[i].poll()
        if poll is not None:
            if poll != 0:
                print("\nError running destroying a deployment:")
                print("stdout:")
                print_proc_lines(destroy_procs[i].stdout)
                print("\nstderr:")
                print_proc_lines(destroy_procs[i].stderr)
            destroy_procs.pop(i)
            continue
        else:
            i += 1

def test_exit(rc: int = 0, timeout = 300):
    t = time.time()
    while len(destroy_procs) > 0 and time.time() - t < timeout:
        check_procs()
        time.sleep(5)
    if len(destroy_procs) > 0:
        print("Not all destroy processes have finished, but timeout was hit, exiting with error")
        sys.exit(1)
    print("All destroy procs have completed, exiting")
    sys.exit(rc)

def run_tests(image_project: str, ramble_file: str, zones: List[str] = ["us-central1-a"],
              img_family: str = None, img_names: str = None, cnt: int = 1, nth: int = 0,
              machine_type: str = "c2-standard-60", num_vms: int = 8):
    if machine_type is None:
        machine_type = "c2-standard-60"
    if cnt is None or cnt <= 0:
        cnt = 1
    if nth is None or nth < 0:
        nth = 0
    if img_names is not None:
        img_names = set(img_names.split(","))
    else:
        img_names = set()
    if num_vms is None or num_vms < 0:
        num_vms = 8

    cmd = "gcloud info"
    res = run_command(cmd, "Error getting gcloud info")
    build_ghpc()
    build_network()
    if img_family is not None and cnt > 0:
        imgs = get_latest_n_images(image_project, img_family, cnt + nth)
    print(imgs)
    img_names.update(imgs[nth:nth+cnt])
    zones = zones.split(",")
    print("Testing the following images:")
    for img in img_names:
        print(img)
    for img in img_names:
        print(f"Starting tests on image: {img}")
        new_zones = zones.copy()
        for cnt, zone in enumerate(zones):
            region = zone[:-2]
            dep_name = create_deployment(image_project, img, zone, region,
                                         ramble_file, machine_type, num_vms)
            dep_dir = os.path.join(RUN_DIR, dep_name)
            res = deploy_tests(dep_dir, image_project, zone, dep_name)
            if res == "":
                print("Tests succeeded, destroying deployment")
                destroy_deployment(dep_dir)
                break
            new_zones.append(new_zones.pop(0))
        zones = new_zones.copy()
        
        if cnt == len(zones)-1:
            print("Could not find a suitable zone to run tests")
            test_exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog='image-tester.py',
                                     description=DESCRIPTION,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("-p", "--image_project", required=True,
                        help="GCP project to get images from")
    parser.add_argument("-r", "--ramble_file", required=True,
                        help="Ramble file location (relative or absolute)")
    parser.add_argument("-f", "--image_family",
                        help="Image family to get images from")
    parser.add_argument("-c", "--num_images", type=int, default = 1,
                        help="Number of images to test (starts from latest " \
                             "in descending order, default = 1)")
    parser.add_argument("-n", "--nth_image", type=int, default = 0,
                        help="Start with nth most recent image (default = 0 == most recent)")
    parser.add_argument("-m", "--machine_type",
                        help="Machine type to run on (default: c2-standard-60)")
    parser.add_argument("-v", "--num_vms", type=int, default=8,
                        help="Number of VMs in testing cluster (default 8)")
    parser.add_argument("-i", "--image_names",
                        help="Comma delimited list of images to test from " \
                             "project")
    parser.add_argument("-z", "--zones",
                        help="Comma delimited list of zones to run test in " \
                             "(default = us-central1-a). They are tried in " \
                             "order until one runs to completion or the list" \
                             " is exhausted")

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)

    args = parser.parse_args()
    if args.image_family is None and args.image_names is None:
        print("--image_family or --image_names must be defined")
        sys.exit(1)

    rf = os.path.abspath(args.ramble_file)

    run_tests(args.image_project, rf, args.zones, args.image_family,
              args.image_names, args.num_images, args.nth_image, 
              args.machine_type, args.num_vms)
    test_exit()
