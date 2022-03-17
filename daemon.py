"""
host daemon that communicates with rentaflop's servers for instructions
functions include, but are not limited to, software updates, system updates,
guest and crypto mining session initiation/termination, uninstallation
usage:
    python daemon.py
    # used to indicate rentaflop code update still in progress so we must call
    # update function again; not used during system updates or on second update
    python daemon.py update
"""
import os
import logging
import uuid
import multiprocessing
from flask import Flask, jsonify, request, abort, redirect
from flask_apscheduler import APScheduler
from config import DAEMON_LOGGER, FIRST_STARTUP, LOG_FILE, REGISTRATION_FILE
from utils import *
import sys
import requests
from requirement_checks import perform_host_requirement_checks


app = Flask(__name__)


def _start_mining():
    """
    starts mining on any stopped GPUs
    """
    # TODO remove after rentaflop miner is on hive; temporarily here to stop nbminer
    run_shell_cmd("miner stop", very_quiet=True)
    state = get_state(available_resources=AVAILABLE_RESOURCES, igd=IGD, gpu_only=True, quiet=True)
    gpus = state["gpus"]
    for gpu in gpus:
        if gpu["state"] == "stopped":
            mine({"type": "crypto", "action": "start", "gpu": gpu["index"]})


def _get_registration(is_checkin=True):
    """
    return registration details from registration file or register if it doesn't exist
    """
    is_registered = os.path.exists(REGISTRATION_FILE)
    daemon_url = "https://portal.rentaflop.com/api/host/daemon"
    rentaflop_id, daemon_port = "", ""

    if is_registered:
        if not is_checkin:
            with open(REGISTRATION_FILE, "r") as f:
                rentaflop_id, wallet_address, daemon_port = f.read().strip().splitlines()
        else:
            rentaflop_id, wallet_address, daemon_port = RENTAFLOP_ID, WALLET_ADDRESS, DAEMON_PORT

    # register host with rentaflop or perform checkin if already registered
    try:
        ip = run_shell_cmd(f'upnpc -u {IGD} -s | grep ExternalIPAddress | cut -d " " -f 3', format_output=False).replace("\n", "")
        if not is_registered:
            daemon_port = select_port(IGD, "daemon")
        data = {"state": get_state(available_resources=AVAILABLE_RESOURCES, igd=IGD), "ip": ip, "port": str(daemon_port), "rentaflop_id": rentaflop_id}
        DAEMON_LOGGER.debug(f"Sent to /api/daemon: {data}")
        response = requests.post(daemon_url, json=data)
        response_json = response.json()
        DAEMON_LOGGER.debug(f"Received from /api/daemon: {response.status_code} {response_json}")
        if not is_registered:
            rentaflop_id = response_json["rentaflop_id"]
    except Exception as e:
        type_str = "checkin" if is_checkin else "registration"
        DAEMON_LOGGER.error(f"Exception: {e}")
        DAEMON_LOGGER.error(f"Failed {type_str}!")
        if is_checkin:
            return
        raise

    # if we just registered, save registration info
    if not is_registered:
        with open(REGISTRATION_FILE, "w") as f:
            f.write(f"{rentaflop_id}\n{daemon_port}")
        DAEMON_LOGGER.debug("Registration successful.")

    return rentaflop_id, daemon_port


def _enable_restart_on_boot():
    """
    places restart in crontab without duplication
    ensures daemon is run on system startup
    """
    # TODO delete function once on hive
    daemon_py = os.path.realpath(__file__)
    # TODO hive specific, use below crontab editing if normal system that doesn't overwrite crontab constantly
    already_restart = run_shell_cmd("grep rentaflop /hive/etc/crontab.root")
    if not already_restart:
        run_shell_cmd(f'printf "\n@reboot python3 {daemon_py}\n" >> /hive/etc/crontab.root')
    run_shell_cmd("crontab /hive/etc/crontab.root")
    # first remove crontab entry if it exists
    # run_shell_cmd(f"crontab -u root -l | grep -v 'python3 {daemon_py}' | crontab -u root -")
    # run_shell_cmd(f'(crontab -u root -l; echo "@reboot python3 {daemon_py}") | crontab -u root -')


def _first_startup():
    """
    run rentaflop installation steps
    """
    # skipping system update
    # list of sources for security updates
    # run_shell_cmd("sudo sh -c 'grep ^deb /etc/apt/sources.list | grep security > /etc/apt/sources.security.only.list'")
    # perform system update
    # update({"type": "system"}, reboot=False)
    # install dependencies
    run_shell_cmd("sudo apt-get install ca-certificates curl gnupg lsb-release -y")
    run_shell_cmd("curl -fsSL https://download.docker.com/linux/debian/gpg \
    | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg --batch --yes")
    run_shell_cmd('echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
    https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null')
    run_shell_cmd("distribution=$(. /etc/os-release; echo $ID$VERSION_ID) \
    && curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add - \
    && curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | sudo tee /etc/apt/sources.list.d/nvidia-docker.list")
    run_shell_cmd("sudo apt-get update -y")
    run_shell_cmd("sudo apt-get install miniupnpc docker-ce docker-ce-cli containerd.io nvidia-docker2 -y")
    # docker setup
    run_shell_cmd("sudo sed -i 's/#no-cgroups = false/no-cgroups = true/' /etc/nvidia-container-runtime/config.toml")
    run_shell_cmd(r'''sudo sed -i '$s/}/,\n"userns-remap":"default"}/' /etc/docker/daemon.json''')
    run_shell_cmd("sudo systemctl restart docker")
    run_shell_cmd("sudo apt-get install iptables-persistent -y")
    run_shell_cmd("sudo apt-get install python3-pip -y && pip3 install speedtest-cli")
    run_shell_cmd("sudo docker build -f Dockerfile -t rentaflop/sandbox .")
    # TODO delete once on hive
    _enable_restart_on_boot()
    run_shell_cmd("sudo reboot")


def _subsequent_startup():
    """
    handle case where log file already exists and we've had a prior daemon startup
    """
    # if update passed as clarg, then we need to call update again to handle situation when
    # update function itself has been updated in the rentaflop code update
    if len(sys.argv) > 1 and sys.argv[1] == "update":
        DAEMON_LOGGER.debug("Entering second update...")
        update({"type": "rentaflop"}, second_update=True)
        DAEMON_LOGGER.debug("Exiting second update.")
        # flushing logs and exiting daemon now since it's set to restart in 3 seconds
        logging.shutdown()
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
    else:
        # error state
        DAEMON_LOGGER.debug("Daemon crashed.")


def _get_available_resources():
    """
    run requirement checks and return dict containing available VM system resources
    """
    passed_checks, gpus = perform_host_requirement_checks()
    if not passed_checks:
        # TODO create min requirement page
        print("Failed minimum requirement checks. Please see our minimum requirement page.")
        DAEMON_LOGGER.error("Failed requirement checks. Exiting...")
        sys.exit(1)
    resources = {"gpu_indexes": gpus}
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
    # set IGD to speed up upnpc commands
    global IGD
    global AVAILABLE_RESOURCES
    global RENTAFLOP_ID
    global WALLET_ADDRESS
    global DAEMON_PORT
    IGD = get_igd()
    AVAILABLE_RESOURCES = _get_available_resources()
    RENTAFLOP_ID, WALLET_ADDRESS, DAEMON_PORT = _get_registration(is_checkin=False)
    # ensure daemon flask server is accessible
    # HTTPS port
    run_shell_cmd(f"upnpc -u {IGD} -e 'rentaflop' -r {DAEMON_PORT} tcp")
    _start_mining()
    # prevent guests from connecting to LAN, run every startup since rules don't seem to stay at top of /etc/iptables/rules.v4
    run_shell_cmd("iptables -I FORWARD -i docker0 -d 192.168.0.0/16 -j DROP")
    run_shell_cmd("iptables -I FORWARD -i docker0 -d 10.0.0.0/8 -j DROP")
    run_shell_cmd("iptables -I FORWARD -i docker0 -d 172.16.0.0/12 -j DROP")
    local_lan_ip = run_shell_cmd(f'upnpc -u {IGD} -s | grep "Local LAN ip address" | cut -d ":" -f 2', format_output=False).strip()
    run_shell_cmd(f"iptables -A INPUT -i docker0 -d {local_lan_ip} -j DROP")
    run_shell_cmd("sudo iptables-save > /etc/iptables/rules.v4")


def mine(params, restart=True):
    """
    handle commands related to mining, whether crypto mining or guest "mining"
    params looks like {"type": "crypto" | "gpc", "action": "start" | "stop", "gpu": "0", "body": contents}
    iff starting gpc, we receive body parameter that contains data to be rendered
    restart indicates whether or not to start crypto job immediately after stopping gpc job
    """
    mine_type = params["type"]
    action = params["action"]
    gpu = int(params["gpu"])
    container_name = f"rentaflop-sandbox-{gpu}"
    
    if action == "start":
        # TODO add pending status to ensure scheduled job doesn't happen to restart crypto mining
        # stop any crypto or gpc job already running
        mine({"type": "gpc", "action": "stop", "gpu": gpu}, restart=False)
        mine({"type": "crypto", "action": "stop", "gpu": gpu})
        gpc_flags = ""
        if mine_type == "gpc":
            username = params["username"]
            password = params["password"]
            gpc_flags = f"--env RENTAFLOP_USERNAME='{username}' --env RENTAFLOP_PASSWORD={password}"

        run_shell_cmd(f"sudo docker run --gpus all --device /dev/nvidia{gpu}:/dev/nvidia0 --device /dev/nvidiactl:/dev/nvidiactl \
        --device /dev/nvidia-modeset:/dev/nvidia-modeset --device /dev/nvidia-uvm:/dev/nvidia-uvm --device /dev/nvidia-uvm-tools:/dev/nvidia-uvm-tools \
        --rm --name {container_name} --env WALLET_ADDRESS={WALLET_ADDRESS} --env RENTAFLOP_ID={RENTAFLOP_ID} --shm-size=256m \
        {gpc_flags} -h rentaflop -dt rentaflop/sandbox")
    elif action == "stop":
        run_shell_cmd(f"docker kill {container_name}")
        if mine_type == "gpc":
            # restart crypto mining if we just stopped a gpc job
            if restart:
                mine({"type": "crypto", "action": "start", "gpu": gpu})


def _stop_all():
    """
    stop all rentaflop docker containers
    """
    containers = run_shell_cmd('docker ps --filter "name=rentaflop*" -q', format_output=False).replace("\n", " ")
    if containers:
        run_shell_cmd(f'docker stop {containers}')
            
            
def update(params, reboot=True, second_update=False):
    """
    handle commands related to rentaflop software and system updates
    params looks like {"type": "rentaflop" | "system"}
    reboot controls whether system update will reboot
    second_update is set to True to indicate current update code running is already up to date,
    False if it hasn't been updated yet
    if second_update is True, we're performing the real update, as latest code for this function
    is currently running, whereas on the first update this function may not have been up to date
    """
    update_type = params["type"]
    if update_type == "rentaflop":
        # must run all commands even if second update
        run_shell_cmd("git pull")
        run_shell_cmd("sudo docker pull rentaflop/deep_learning:latest")
        run_shell_cmd("sudo docker build -f Dockerfile -t rentaflop/sandbox .")
        update_param = "" if second_update else " update"
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
    run_shell_cmd(f"upnpc -u {IGD} -d {DAEMON_PORT} tcp")
    daemon_py = os.path.realpath(__file__)
    run_shell_cmd("sed -i '/rentaflop/d' /hive/etc/crontab.root")
    run_shell_cmd(f"crontab -u root -l | grep -v 'python3 {daemon_py}' | crontab -u root -")
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
    return {"state": get_state(available_resources=AVAILABLE_RESOURCES, igd=IGD)}


@app.before_request
def before_request():
    # don't allow anyone who isn't rentaflop to communicate with host daemon
    # only people who know a host's rentaflop id are the host and rentaflop
    # TODO check size first to prevent DOS attack
    request_json = request.get_json()
    request_rentaflop_id = request_json.get("rentaflop_id", "")
    if request_rentaflop_id != RENTAFLOP_ID:
        return abort(403)
    
    # force https
    if not request.is_secure:
        url = request.url.replace('http://', 'https://', 1)
        code = 301

        return redirect(url, code=code)


def run_flask_server(q):
    @app.route("/", methods=["POST"])
    def index():
        request_json = request.get_json()
        cmd = request_json.get("cmd")
        params = request_json.get("params")
        func = CMD_TO_FUNC.get(cmd)
        finished = False
        if func:
            try:
                finished = log_before_after(func, params)()
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
}
IGD = None
RENTAFLOP_ID = None
WALLET_ADDRESS = None
DAEMON_PORT = None
AVAILABLE_RESOURCES = None


def main():
    _handle_startup()
    app.secret_key = uuid.uuid4().hex
    # create a scheduler that periodically checks for stopped GPUs and starts mining on them; periodic checkin to rentaflop servers
    scheduler = APScheduler()
    scheduler.add_job(id='Start Miners', func=_start_mining, trigger="interval", seconds=300)
    scheduler.add_job(id='Rentaflop Checkin', func=_get_registration, trigger="interval", seconds=3600)
    scheduler.start()
    # run server, allowing it to shut itself down
    q = multiprocessing.Queue()
    server = multiprocessing.Process(target=run_flask_server, args=(q,))
    server.start()
    finished = q.get(block=True)
    if finished:
        server.terminate()
        DAEMON_LOGGER.debug("Stopping daemon.")
        logging.shutdown()


if __name__=="__main__":
    main()
