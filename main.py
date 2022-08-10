"""
main code for rentaflop miner daemon
"""
import os
import logging
import uuid
import multiprocessing
from flask import jsonify, request, abort, redirect
from flask_apscheduler import APScheduler
from config import DAEMON_LOGGER, FIRST_STARTUP, LOG_FILE, REGISTRATION_FILE, DAEMON_PORT, get_app_db
from utils import *
import sys
import requests
from requirement_checks import perform_host_requirement_checks
import json
import socket
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import time
import traceback
import subprocess
from threading import Thread
app, db = get_app_db()


def _start_mining(startup=False):
    """
    starts mining on any stopped GPUs
    startup will sleep for several seconds before attempting to start mining, as if this is a miner restart the old containers
    about to die may still be running
    """
    # if just started, wait for gpus to "wake up" on boot
    if startup:
        time.sleep(10)
    state = get_state(available_resources=RENTAFLOP_CONFIG["available_resources"], gpu_only=True, quiet=True)
    gpus = state["gpus"]
    gpus_stopped = {gpu["index"] for gpu in gpus if gpu["state"] == "stopped"}
    gpus_stopped_later = gpus_stopped
    # we want to make sure we're not starting miner back up right before a task is about to be run so we try again before restarting
    if gpus_stopped and not startup:
        time.sleep(10)
        state = get_state(available_resources=RENTAFLOP_CONFIG["available_resources"], gpu_only=True, quiet=True)
        gpus = state["gpus"]
        gpus_stopped_later = {gpu["index"] for gpu in gpus if gpu["state"] == "stopped"}
    
    for gpu_index in gpus_stopped.intersection(gpus_stopped_later):
        mine({"action": "start", "gpu": gpu_index})


def _get_registration(is_checkin=True):
    """
    return registration details from registration file or register if it doesn't exist
    """
    config_changed = False
    if not is_checkin:
        with open(REGISTRATION_FILE, "r") as f:
            rentaflop_config = json.load(f)
            # we only save things we need to detect changes on in the registration file
            # this is because rentaflop db saves these so we need to know when to update it
            rentaflop_id = rentaflop_config.get("rentaflop_id", "")
            wallet_address = rentaflop_config.get("wallet_address", "")
            daemon_port = rentaflop_config.get("daemon_port", 0)
            email = rentaflop_config.get("email", "")
            sandbox_id = rentaflop_config.get("sandbox_id", "")
            current_email, disable_crypto, current_wallet_address, pool_url, hash_algorithm, password = get_custom_config()
            if current_email != email and current_email:
                email = current_email
                config_changed = True
            if current_wallet_address != wallet_address and current_wallet_address:
                wallet_address = current_wallet_address
                config_changed = True
            if daemon_port != DAEMON_PORT:
                config_changed = True
            crypto_config = {"wallet_address": wallet_address, "email": email, "disable_crypto": disable_crypto, "pool_url": pool_url, \
                             "hash_algorithm": hash_algorithm, "pass": password}
    else:
        rentaflop_id, sandbox_id, crypto_config = RENTAFLOP_CONFIG["rentaflop_id"], RENTAFLOP_CONFIG["sandbox_id"], RENTAFLOP_CONFIG["crypto_config"]

    # rentaflop id is either read from the file, already set if it's a checkin, or is initial registration where it's empty str
    is_registered = rentaflop_id != ""
    # sometimes file read appears to fail and we erroneously create a new registration, so this prevents it
    if not is_registered:
        # TODO figure out a better way to do this without reading log file (perhaps server handles it by checking if ip address and devices already registered)
        registrations = run_shell_cmd(f"cat {LOG_FILE} | grep 'Registration successful.'", format_output=False, very_quiet=True)
        registrations = [] if not registrations else registrations.splitlines()
        # we've already registered and logged it, so file was read incorrectly and we should try again
        if len(registrations) > 0:
            DAEMON_LOGGER.error("Rentaflop id not set but found successful registration, retrying...")
            time.sleep(30)
            
            return _get_registration(is_checkin=is_checkin)
    
    # using external website to get ip address
    try:
        ip = requests.get('https://api.ipify.org').content.decode('utf8')
    except requests.exceptions.ConnectionError:
        ip = None
    # register host with rentaflop or perform checkin if already registered
    data = {"state": get_state(available_resources=RENTAFLOP_CONFIG["available_resources"], quiet=is_checkin), "ip": ip, \
            "rentaflop_id": rentaflop_id, "email": crypto_config["email"], "wallet_address": crypto_config["wallet_address"]}
    response_json = post_to_rentaflop(data, "daemon", quiet=is_checkin)
    if response_json is None:
        type_str = "checkin" if is_checkin else "registration"
        DAEMON_LOGGER.error(f"Failed {type_str}!")
        if is_checkin:
            return {}
        if is_registered:
            return rentaflop_id, sandbox_id, crypto_config
        raise
    elif is_checkin:
        return response_json
    
    if not is_registered:
        rentaflop_id = response_json["rentaflop_id"]
        sandbox_id = response_json["sandbox_id"]
        config_changed = True

    # if we just registered or changed config, save registration info
    if config_changed:
        # still saving daemon port because it's used by h-stats.sh to query status
        update_config(rentaflop_id, DAEMON_PORT, sandbox_id, crypto_config["wallet_address"], crypto_config["email"])
        # don't change this without also changing the grep search for this string above
        if not is_registered:
            DAEMON_LOGGER.debug("Registration successful.")

    return rentaflop_id, sandbox_id, crypto_config


def _handle_checkin():
    """
    handles checkins with rentaflop servers and executes instructions returned
    """
    # instruction looks like {"cmd": ..., "params": ..., "rentaflop_id": ...}
    instruction_json = _get_registration()
    # if no instruction, do nothing
    if not instruction_json:
        return
    # hand off instruction to localhost web server
    files = {"json": json.dumps(instruction_json)}
    requests.post(f"https://localhost:{DAEMON_PORT}", files=files, verify=False)


def _first_startup():
    """
    run rentaflop installation steps
    """
    install_all_requirements()
    # disable reboot during driver check/installation because we reboot on next command
    check_correct_driver(reboot=False)
    run_shell_cmd("sudo reboot")


def _subsequent_startup():
    """
    handle case where log file already exists and we've had a prior daemon startup
    """
    # if update passed as clarg, then we need to call update again to handle situation when
    # update function itself has been updated in the rentaflop code update
    if len(sys.argv) > 1:
        if sys.argv[1] == "update":
            DAEMON_LOGGER.debug("Entering second update...")
            target_version = "" if len(sys.argv) < 3 else sys.argv[2]
            update({"type": "rentaflop", "target_version": target_version}, second_update=True)
            DAEMON_LOGGER.debug("Exiting second update.")
            # flushing logs and exiting daemon now since it's set to restart in 3 seconds
            logging.shutdown()
            sys.exit(0)
        elif sys.argv[1] == "sleep":
            time.sleep(5)
            sys.exit(0)

    # get last line of log file
    with open(LOG_FILE, 'rb') as f:
        # catch OSError in case of a one line file
        try:
            f.seek(-2, os.SEEK_END)
            while f.read(1) != b'\n':
                f.seek(-2, os.SEEK_CUR)
        except OSError:
            f.seek(0)
        last_line = f.readline().decode()

    is_update = ("sudo reboot" in last_line) or ("python3 daemon.py" in last_line) or \
        ("Exiting second update." in last_line)
    if is_update:
        DAEMON_LOGGER.debug("Exiting update.")
        # ensure anything that started up during update gets killed
        kill_other_daemons()
    elif "Exiting update." not in last_line and "Stopping daemon." not in last_line:
        # error state
        DAEMON_LOGGER.debug("Daemon crashed.")


def _get_available_resources():
    """
    run requirement checks and return dict containing available VM system resources
    """
    passed_checks, resources = perform_host_requirement_checks()
    if not passed_checks:
        print("Failed minimum requirement checks. Please see our minimum system requirements at https://portal.rentaflop.com/blog/hosting")
        DAEMON_LOGGER.error("Failed requirement checks!")
        raise Exception(f"Failed requirement checks! Available GPUS: {resources}")
    
    DAEMON_LOGGER.debug(f"Finished requirement checks, found available resources: {resources}")

    return resources


def _handle_startup():
    """
    uses log file existence to handle startup scenarios
    if no log file, then assume first startup
    if first startup, run rentaflop installation steps
    if log file exists, check last command to see if it was an update
    if not update, assume crash and error state
    if update, log update completed
    """
    # NOTE: this if else must be first as we need to immediately check last line in log file during updates
    if FIRST_STARTUP:
        _first_startup()
    else:
        _subsequent_startup()

    DAEMON_LOGGER.debug("Starting daemon...")
    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    run_shell_cmd("sudo nvidia-smi -pm 1", quiet=True)
    run_shell_cmd("./nvidia_uvm_init.sh", quiet=True)
    global RENTAFLOP_CONFIG
    RENTAFLOP_CONFIG["available_resources"] = _get_available_resources()
    RENTAFLOP_CONFIG["rentaflop_id"], RENTAFLOP_CONFIG["sandbox_id"], RENTAFLOP_CONFIG["crypto_config"] = \
        _get_registration(is_checkin=False)
    # must do installation check before anything required by it is used
    check_installation()
    oc_settings, oc_hash = get_oc_settings()
    # db table contains original (set by user in hive) oc settings and hash of current (not necessarily original) oc settings
    write_oc_settings(oc_settings, oc_hash, db)
    DAEMON_LOGGER.debug(f"Found OC settings: {oc_settings}")
    # prevent guests from connecting to LAN, run every startup since rules don't seem to stay at top of /etc/iptables/rules.v4
    # TODO this is breaking internet connection for some reason, ensure docker img can't connect to host
    # run_shell_cmd("iptables -I FORWARD -i docker0 -d 192.168.0.0/16 -j DROP")
    run_shell_cmd("iptables -I FORWARD -i docker0 -d 10.0.0.0/8 -j DROP")
    run_shell_cmd("iptables -I FORWARD -i docker0 -d 172.16.0.0/12 -j DROP")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    local_lan_ip = s.getsockname()[0]
    s.close()
    run_shell_cmd(f"iptables -A INPUT -i docker0 -d {local_lan_ip} -j DROP")
    run_shell_cmd("sudo iptables-save > /etc/iptables/rules.v4")
    if not RENTAFLOP_CONFIG["crypto_config"]["disable_crypto"]:
        _start_mining(startup=True)


def _run_sandbox(gpu, container_name, timeout=0):
    """
    runs docker sandbox based on parameters; does nothing if container_name already running
    checks run command output; if None, it means docker threw an exception caught by run_shell_cmd and we should retry since it sometimes fails on first try
    if timeout is set, kill docker after timeout seconds (duration of 0 disables timeout)
    """
    # check for existing container running, avoid grep because of substring issues; $ after container name prevents substring issues
    output = run_shell_cmd(f'docker ps --filter "name={container_name}$" --format "{{.Names}}"', quiet=True)
    if output:
        # already running so just return ip
        container_ip = run_shell_cmd("docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "+container_name, format_output=False).strip()
        
        return container_ip

    tries = 2
    for _ in range(tries):
        output = run_shell_cmd(f"sudo docker run --gpus all --device /dev/nvidia{gpu}:/dev/nvidia0 --device /dev/nvidiactl:/dev/nvidiactl \
        --device /dev/nvidia-modeset:/dev/nvidia-modeset --device /dev/nvidia-uvm:/dev/nvidia-uvm --device /dev/nvidia-uvm-tools:/dev/nvidia-uvm-tools \
        --rm --name {container_name} --env SANDBOX_ID={RENTAFLOP_CONFIG['sandbox_id']} --env GPU={gpu} --env TIMEOUT={timeout} --shm-size=256m -h rentaflop -dt rentaflop/sandbox")
        if output:
            container_ip = run_shell_cmd("docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "+container_name, format_output=False).strip()
            wait_for_sandbox_server(container_ip)

            return container_ip


def mine(params):
    """
    handle commands related to mining, whether crypto mining or guest "mining"
    params looks like {"action": "start" | "stop", "gpu": "0", "task_id": "13245", "render_file": contents}
    iff render job, we receive task_id and render_file parameter (if action is start) that contains data to be rendered
    """
    action = params["action"]
    gpu = int(params["gpu"])
    task_id = params.get("task_id")
    start_frame = params.get("start_frame")
    n_frames = params.get("n_frames")
    render_file = params.get("render_file")
    container_name = f"rentaflop-sandbox-{gpu}"
    is_render = False
    if task_id:
        is_render = True
    
    if action == "start":
        if is_render:
            stop_crypto_miner(gpu)
            disable_oc([gpu])
            # ensure sandbox for gpu is running, does nothing if already running
            container_ip = _run_sandbox(gpu, container_name)
            url = f"https://{container_ip}"
            end_frame = start_frame + n_frames - 1
            data = {"cmd": "push", "params": {"task_id": task_id, "start_frame": start_frame, "end_frame": end_frame}}
            files = {'render_file': render_file, 'json': json.dumps(data)}
            post_to_sandbox(url, files)
        else:
            if RENTAFLOP_CONFIG["crypto_config"]["disable_crypto"]:
                return
            run_shell_cmd(f"docker stop {container_name}", very_quiet=True)
            # 4059 is default port from hive
            crypto_port = 4059 + gpu
            hostname = socket.gethostname()
            enable_oc([gpu])
            # does nothing if already mining
            start_crypto_miner(gpu, crypto_port, hostname, RENTAFLOP_CONFIG["crypto_config"])
    elif action == "stop":
        if is_render:
            container_ip = run_shell_cmd("docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "+container_name, format_output=False).strip()
            url = f"https://{container_ip}"
            data = {"cmd": "pop", "params": {"task_id": task_id}}
            files = {'json': json.dumps(data)}
            post_to_sandbox(url, files)
            enable_oc([gpu])
        else:
            stop_crypto_miner(gpu)


def _stop_all():
    """
    stop all rentaflop docker containers and crypto mining processes
    """
    DAEMON_LOGGER.debug("Stopping containers...")
    # stops both sandbox and benchmark containers
    containers = run_shell_cmd('docker ps --filter "name=rentaflop*" -q', format_output=False).replace("\n", " ")
    if containers:
        # have to use subprocess here to properly run in bg; run in bg because this command takes over 10 seconds
        arg_list = ["docker", "stop"]
        arg_list.extend(containers.split())
        subprocess.Popen(arg_list)    
    run_shell_cmd('killall t-rex')
    run_shell_cmd('killall octane')
    DAEMON_LOGGER.debug("Containers stopped.")
            
            
def update(params, reboot=True, second_update=False):
    """
    handle commands related to rentaflop software and system updates
    params looks like {"type": "rentaflop" | "system", "target_version": "abc123"}
    target_version is git version to update to when type is rentaflop; if not set, we update to latest master
    reboot controls whether system update will reboot
    second_update is set to True to indicate current update code running is already up to date,
    False if it hasn't been updated yet
    if second_update is True, we're performing the real update, as latest code for this function
    is currently running, whereas on the first update this function may not have been up to date
    """
    update_type = params["type"]
    if update_type == "rentaflop":
        # must run all commands even if second update
        target_version = params.get("target_version", "")
        # use test branch develop if testing on rentaflop_one otherwise use prod branch master
        branch = "develop" if socket.gethostname() in ["rentaflop_one", "rentaflop_two"] else "master"
        run_shell_cmd(f"git checkout {branch}")
        run_shell_cmd("git pull")
        if target_version:
            run_shell_cmd(f"git checkout {target_version}")
        run_shell_cmd("sudo docker pull rentaflop/host:latest")
        run_shell_cmd("sudo docker build -f Dockerfile -t rentaflop/sandbox .")
        # ensure all old containers are stopped so we can run new ones with latest code
        _stop_all()
        update_param = "" if second_update else f" update {target_version}"
        # ensure a daemon is still running during an update; prevents hive from trying to restart it itself
        subprocess.Popen(["python3", "daemon.py", "sleep"])
        # daemon will shut down (but not full system) so this ensures it starts back up again
        run_shell_cmd(f'echo "sleep 3; python3 daemon.py{update_param}" | at now')

        return True
    elif update_type == "system":
        run_shell_cmd("sudo apt-get update -y")
        # perform only security updates
        run_shell_cmd(r'''DEBIAN_FRONTEND=noninteractive \
        sudo apt-get -s dist-upgrade -y -o Dir::Etc::SourceList=/etc/apt/sources.security.only.list \
        -o Dir::Etc::SourceParts=/dev/null  | grep "^Inst" | awk -F " " {'print $2'}''')
        if reboot:
            run_shell_cmd("sudo reboot")
        

def uninstall(params):
    """
    uninstall rentaflop from this machine
    """
    # stop and remove all rentaflop docker containers and images
    _stop_all()
    run_shell_cmd('docker rmi $(docker images -a -q "rentaflop/sandbox") $(docker images | grep none | awk "{ print $3; }") $(docker images "nvidia/cuda" -a -q)')
    # clean up rentaflop host software
    daemon_py = os.path.realpath(__file__)
    rentaflop_miner_dir = os.path.dirname(daemon_py)
    run_shell_cmd(f"rm -rf {rentaflop_miner_dir}", quiet=True)

    return True


def send_logs(params):
    """
    gather host logs and send back to rentaflop servers
    """
    with open(LOG_FILE, "r") as f:
        logs = f.readlines()
        # remove trailing newlines and empty lines
        logs = [log[:-1] for log in logs if not log.isspace()]

    return {"logs": logs}


def status(params):
    """
    return the state of this host
    """
    return {"state": get_state(available_resources=RENTAFLOP_CONFIG["available_resources"], quiet=True)}


def benchmark(params):
    """
    run performance benchmark for gpus
    """
    gpu_indexes = RENTAFLOP_CONFIG["available_resources"]["gpu_indexes"]
    gpu_indexes = [int(gpu) for gpu in gpu_indexes]
    _stop_all()
    disable_oc(gpu_indexes)
    for gpu in gpu_indexes:
        container_name = f"rentaflop-benchmark-{gpu}"
        # start container for benchmarking; 15 minute timeout (900 seconds)
        container_ip = _run_sandbox(gpu, container_name, timeout=900)
        url = f"https://{container_ip}/benchmark"
        # sending empty post request for now, at some point will issue challenges to prove benchmark results
        data = {}
        files = {'json': json.dumps(data)}
        post_to_sandbox(url, files)


def prep_daemon_shutdown(server):
    """
    prepare daemon for shutdown without assuming system is restarting
    stops all mining jobs and terminates server
    """
    _stop_all()
    gpu_indexes = RENTAFLOP_CONFIG["available_resources"]["gpu_indexes"]
    gpu_indexes = [int(gpu) for gpu in gpu_indexes]
    # make sure we restore oc settings back to original
    enable_oc(gpu_indexes)
    DAEMON_LOGGER.debug("Stopping server...")
    time.sleep(5)
    if server:
        server.terminate()
    DAEMON_LOGGER.debug("Stopping daemon.")
    logging.shutdown()


def clean_logs(clear_contents=True, error=None):
    """
    send logs to rentaflop servers and clear contents of logs, leaving an 1-line file indicating registration
    """
    logs = send_logs({})
    if error:
        logs["error"] = error
    if RENTAFLOP_CONFIG["rentaflop_id"]:
        logs["rentaflop_id"] = RENTAFLOP_CONFIG["rentaflop_id"]
    post_to_rentaflop(logs, "logs")
    if clear_contents:
        with open(LOG_FILE, "w") as f:
            # must write this because of check in _get_registration
            f.write("Registration successful.")

    
@app.before_request
def before_request():
    # don't allow anyone who isn't rentaflop to communicate with host daemon
    # only people who know a host's rentaflop id are the host and rentaflop
    # file size check in app config
    json_file = request.files.get("json")
    request_json = json.loads(json_file.read())
    json_file.seek(0)
    request_rentaflop_id = request_json.get("rentaflop_id", "")
    if request_rentaflop_id != RENTAFLOP_CONFIG["rentaflop_id"]:
        return abort(403)
    
    # force https
    if not request.is_secure:
        url = request.url.replace('http://', 'https://', 1)
        code = 301

        return redirect(url, code=code)


def run_flask_server(q):
    @app.route("/", methods=["POST"])
    def index():
        request_json = json.loads(request.files.get("json").read())
        cmd = request_json.get("cmd")
        params = request_json.get("params")
        render_file = request.files.get("render_file")
        if render_file:
            params["render_file"] = render_file
        
        func = CMD_TO_FUNC.get(cmd)
        finished = False
        if func:
            try:
                if cmd != "status":
                    func_log = log_before_after(func, params)
                    if cmd == "benchmark":
                        Thread(target=func_log).start()
                    else:
                        finished = func_log()
                else:
                    # avoid logging on status since this is called every 10 seconds by hive stats checker
                    finished = func(params)
            except Exception as e:
                DAEMON_LOGGER.exception(f"Caught exception: {e}")
        if finished is True:
            q.put(finished)
        # finished isn't True but it's not Falsey, so return it in response
        if (finished is not True) and finished:
            return jsonify(finished), 200

        return jsonify("200")
    
    app.run(host='0.0.0.0', port=DAEMON_PORT, ssl_context='adhoc')
    
    
CMD_TO_FUNC = {
    "mine": mine,
    "update": update,
    "uninstall": uninstall,
    "send_logs": send_logs,
    "status": status,
    "benchmark": benchmark
}
# rentaflop config looks like {"rentaflop_id": ..., "sandbox_id": ..., \
# "available_resources": {"gpu_indexes": [...], "gpu_names": [...]}, "crypto_config": {"wallet_address": ..., \
# "email": ..., "disable_crypto": ..., "pool_url": ..., "hash_algorithm": ..., "pass": ...}}
RENTAFLOP_CONFIG = {"rentaflop_id": None, "sandbox_id": None, "available_resources": {}, \
                    "crypto_config": {}}


def main():
    try:
        server = None
        _handle_startup()
        app.secret_key = uuid.uuid4().hex
        # create a scheduler that periodically checks for stopped GPUs and starts mining on them; periodic checkin to rentaflop servers
        scheduler = APScheduler()
        if not RENTAFLOP_CONFIG["crypto_config"]["disable_crypto"]:
            scheduler.add_job(id='Start Miners', func=_start_mining, trigger="interval", seconds=60)
        scheduler.add_job(id='Rentaflop Checkin', func=_handle_checkin, trigger="interval", seconds=60)
        scheduler.add_job(id='Clean Logs', func=clean_logs, trigger="interval", minutes=60*24*7)
        scheduler.start()
        # run server, allowing it to shut itself down
        q = multiprocessing.Queue()
        server = multiprocessing.Process(target=run_flask_server, args=(q,))
        DAEMON_LOGGER.debug("Starting server...")
        server.start()
        finished = q.get(block=True)
        if finished:
            DAEMON_LOGGER.info("Daemon shutting down for update...")
            prep_daemon_shutdown(server)
    except KeyboardInterrupt:
        DAEMON_LOGGER.info("Daemon stopped by Hive...")
        prep_daemon_shutdown(server)
    except SystemExit:
        # ignoring intentional system exits and allowing daemon to shut itself down
        pass
    except:
        error = traceback.format_exc()
        DAEMON_LOGGER.error(f"Entering update loop because of uncaught exception: {error}")
        # send logs and error data to rentaflop servers
        clean_logs(clear_contents=False, error=error)
        # ensure all requirements are installed in case something broke during first run or an update with new requirements
        install_all_requirements()
        # don't loop too fast
        time.sleep(180)
        # handle runtime errors and other issues by performing an update, preventing most bugs from breaking a rentaflop installation
        update({"type": "rentaflop"})
