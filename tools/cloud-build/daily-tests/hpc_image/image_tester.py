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
import sys
import uuid
import tarfile
import argparse
import shutil
import subprocess

# TODO Replace gcloud commands with gcloud pip module commands
ROOT_DIR = sys.path[0]
BUILD_PROJECT_ID = "cloud-hpc-image-devel"
GHPC_DIR = os.path.join(ROOT_DIR, "hpc-toolkit")
GHPC_BIN_SRC = os.path.join(GHPC_DIR, "ghpc")
GHPC_BIN_DST = os.path.join(ROOT_DIR, "ghpc")
NET_NAME = "hpc-image-testing-net"
NET_BP = os.path.join(ROOT_DIR, "testing-vpc.yaml")
TEMPLATE_BP = os.path.join(ROOT_DIR, "intel-mpi-bm-template.yaml")
NET_DEP_FOLDER = "hpc-image-vpc"
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

def run_command(cmd: str, err_msg: str = None) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                         check=False, env=ENV_COPY)
    if res.returncode != 0:
        raise subprocess.SubprocessError(f"{err_msg}:\n{res.stderr}")
    return res

def install_go(go_ver: str = "1.21.5") -> None:
    if os.path.exists("go"):
        return
    print("Golang not found on system, attempting to install precompiled package")
    fn = f"go{go_ver}.linux-amd64.tar.gz"
    abs_fn = os.path.join(ROOT_DIR, fn)
    cmd = f"curl -OL https://golang.org/dl/{fn}"
    run_command(cmd, "Error downloading Go package")
    with open(abs_fn, mode="rb") as fp:
        tf = tarfile.open(fileobj=fp)
        tf.extractall(ROOT_DIR)
    os.remove(abs_fn)
    ENV_COPY['PATH'] = f"{ROOT_DIR}/go/bin:{ENV_COPY['PATH']}"
    print("Golang installed")

def get_latest_n_images(project: str, img_family: str, n: int = 3) -> None:
    cmd = f"gcloud compute images list --project={project} " \
          "--no-standard-images --show-deprecated " \
          f"--filter=\"family={img_family}\" --format='value(name)' " \
          f"--sort-by=~name --limit {n}"
    err_msg = f"Error getting last {n} images from image family {img_family}"
    res = run_command(cmd, err_msg)
    if res.stdout is not None:
        imgs = res.stdout.split()
        if len(imgs) < n:
            print(f"WARNING: Only {len(imgs)} images were found in family " \
                  f"{img_family}")
        return imgs
    print(f"No images were found for the {img_family} family, exiting")
    sys.exit()

def get_data(image_name: str, deployment_name: str, output_dir: str) -> None:
    cmd = "gcloud compute scp --recurse --project {project} --zone {zone} " \
          f"{deployment_name}-0:/shared/test_workspace/experiments/intel-mpi-benchmarks/* " \
          f"{output_dir}/{image_name} </dev/null"
    run_command(cmd, "Error downloading Ramble test results")

def build_ghpc(branch: str = "develop") -> None:
    if os.path.exists(GHPC_BIN_DST):
        print("GHPC already in the correct location, not rebuilding")
        return
    if os.path.exists(GHPC_DIR):
        shutil.rmtree(GHPC_DIR)
    install_go()
    print("Cloning HPC Toolkit")
    cmd = f"git clone https://github.com/GoogleCloudPlatform/hpc-toolkit.git {GHPC_DIR}"
    run_command(cmd, "Error cloning HPC Toolkit repo")
    cmd = f"git checkout origin/{branch}"
    os.chdir(GHPC_DIR)
    print(f"Checking out {branch} branch")
    run_command(cmd, f"Error checking out to {branch} branch")
    print("Building ghpc")
    run_command("make", "Error making ghpc")
    shutil.copy(GHPC_BIN_SRC, GHPC_BIN_DST)
    os.chdir(ROOT_DIR)

def build_network() -> None:
    # TODO: Check that we have a subnetwork in the correct zone/region
    cmd = f"gcloud compute networks list --project={BUILD_PROJECT_ID} " \
          f"--format='value(name)' --filter='name:{NET_NAME}'"
    res = run_command(cmd, "Error getting list of gcloud networks")
    if res.stdout is None:
        cmd = f"./ghpc create -w --vars project_id={BUILD_PROJECT_ID} {NET_BP}"
        run_command(cmd, "Error creating network deployment folder")
        cmd = f"./ghpc deploy {NET_DEP_FOLDER} --auto-approve"
        run_command(cmd, "Error deploying network")

def create_deployment(project:str, image_name: str, zone: str,
                      ramble_file: str,
                      machine_type: str = "c2-standard-60") -> str:
    if "BUILD_ID" in os.environ:
        build_id = os.environ["BUILD_ID"][:6]
    else:
        build_id = uuid.uuid4().hex[:6]
    dep_name=f"test-{image_name}-{build_id}"
    dep_vars = [f"ramble_config_location={ramble_file}",
                "add_deployment_name_before_prefix=true",
                f"compute_machine_type={machine_type}",
                f"image_project={project}",
                f"image_name={image_name}",
                f"region={zone[:-2]}",
                f"zone={zone}",
                f"deployment_name={dep_name}"]
    cmd = f"./ghpc create -w --vars project_id={BUILD_PROJECT_ID} " \
          f"--vars {','.join(dep_vars)} {TEMPLATE_BP}"
    run_command(cmd, "Error creating microbenchmark deployment")
    return dep_name

def deploy_tests(dep_dir: str) -> None:
    cmd = f"./ghpc deploy {dep_dir} --auto-approve"
    try:
        run_command(cmd, "Error deploying microbenchmark tests")
    except subprocess.SubprocessError as e:
        print(f"{e}\nDestroying deployment then exiting")
        destroy_deployment(dep_dir)
        sys.exit()

def destroy_deployment(dep_dir: str) -> None:
    cmd = f"./ghpc destroy {dep_dir} --auto-approve"
    run_command(cmd, "Error destroying microbenchmark deployment")

def run_tests(project: str, zone: str, ramble_file: str,
              img_family: str = None, img_names: str = None, n: int = 3,
              machine_type: str = "c2-standard-60"):
    if machine_type is None:
        machine_type = "c2-standard-60"
    if n is None or n <= 0:
        n = 3
    if img_names is not None:
        img_names = set(img_names.split(","))
    else:
        img_names = set()
    cmd = "gcloud info"
    res = run_command(cmd, "Error getting gcloud info")
    print(res.stdout)
    build_ghpc()
    build_network()
    img_names.update(get_latest_n_images(project, img_family, n))
    print("Testing the following images:")
    for img in img_names:
        print(img)
    for img in img_names:
        print(f"Starting tests on image: {img}")
        print("Creating deployment")
        rel_dep_dir = create_deployment(project, img, zone, ramble_file, machine_type)
        dep_dir = os.path.join(ROOT_DIR, rel_dep_dir)
        print("Deploying tests")
        deploy_tests(dep_dir)
        print("Destroying tests")
        destroy_deployment(dep_dir)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog='hpc-image-tester.py',
                                     description=DESCRIPTION,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("-p", "--project", required=True,
                        help="GCP project to get images from")
    parser.add_argument("-r", "--ramble_file", required=True,
                        help="Ramble file location (relative or absolute)")
    parser.add_argument("-f", "--image_family",
                        help="Image family to get images from")
    parser.add_argument("-n", "--num_images", type=int,
                        help="Number of images to test (starts from latest " \
                             "in descending order, default = 3)")
    parser.add_argument("-m", "--machine_type",
                        help="Machine type to run on (default: c2-standard-60)")
    parser.add_argument("-i", "--image_names",
                        help="Comma delimited list of images to test from " \
                             "project")
    parser.add_argument("-z", "--zone",
                        help="Zone to run test in (default = us-central1-a)")

    if len(sys.argv)==1:
        parser.print_help(sys.stderr)
        sys.exit(1)

    args = parser.parse_args()
    if args.image_family is None and args.image_names is None:
        print("--image_family or --image_names must be defined")
        sys.exit()

    rf = os.path.abspath(args.ramble_file)

    run_tests(args.project, args.zone, rf, args.image_family,
              args.image_names, args.num_images, args.machine_type)
