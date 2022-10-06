"""
runs render task
usage:
    # task_dir is directory containing render file for task
    python3 run.py task_dir start_frame end_frame uuid_str
"""
import sys
import os
import requests
import json
from config import DAEMON_LOGGER
import subprocess


def main():
    try:
        task_dir = sys.argv[1]
        start_frame = sys.argv[2]
        end_frame = sys.argv[3]
        uuid_str = sys.argv[4]
        output_path = os.path.join(task_dir, "output/")
        os.mkdir(output_path)
        os.system(f"touch {task_dir}/started.txt")
        render_path = f"{task_dir}/render_file.blend"
        render_path2 = f"{task_dir}/render_file2.blend"
        script1 = f'''"import os; os.system('gpg --passphrase {uuid_str} --batch --no-tty -d {render_path} > {render_path2} && mv {render_path2} {render_path}')"'''
        script2 = f'''"import os; os.remove('{render_path}')"'''
        render_config = subprocess.check_output(f"blender/blender --python-expr {script1} -b {render_path} --python render_config.py", shell=True,
                                                encoding="utf8", stderr=subprocess.STDOUT)
        is_eevee = "Found render engine: BLENDER_EEVEE" in render_config

        # render results for specified frames to output path; disables scripting; if eevee is specified in blend file then it'll use eevee, even though cycles is specified here
        cmd = f"DISPLAY=:0.0 blender/blender -b {render_path} --python-expr {script2} -o {output_path} -s {start_frame} -e {end_frame} --disable-autoexec -a -- --cycles-device OPTIX"
        return_code = os.system(cmd)
        # successful render, so send result to servers
        if return_code == 0:
            tgz_path = os.path.join(task_dir, "output.tar.gz")
            output = os.path.join(task_dir, "output")
            # zip and send output dir
            os.system(f"tar -czf {tgz_path} {output}")
            sandbox_id = os.getenv("SANDBOX_ID")
            server_url = "https://api.rentaflop.com/host/output"
            task_id = os.path.basename(task_dir)
            # first request to get upload location
            data = {"task_id": str(task_id), "sandbox_id": str(sandbox_id)}
            response = requests.post(server_url, json=data)
            response_json = response.json()
            storage_url, fields = response_json["url"], response_json["fields"]
            # upload output to upload location
            with open(tgz_path, 'rb') as f:
                files = {'file': f}
                storage_response = requests.post(storage_url, data=fields, files=files)

            # confirm upload
            data["confirm"] = True
            if is_eevee:
                data["is_eevee"] = True
            requests.post(server_url, json=data)
        else:
            DAEMON_LOGGER.error(f"Task execution command failed with code {return_code}!")
    except:
        error = traceback.format_exc()
        DAEMON_LOGGER.error(f"Exception during task execution: {error}")
    finally:
        # lets the task queue know when the run is finished
        os.system(f"touch {task_dir}/finished.txt")

    
if __name__=="__main__":
    main()
