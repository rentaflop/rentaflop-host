#!/usr/bin/env bash

. colors

if [[ $1 == "start" ]]; then
	echo ""
	echo "${CYAN}> Starting services${NOCOLOR}"
	hivex start >/dev/null 2>&1
	nvidia-oc > /dev/null 2>&1
	amd-oc > /dev/null 2>&1
	autofan start > /dev/null 2>&1
	exit
fi


[[ -z "$1" ]] && try_count=3 || try_count="$1"

echo ""
echo "${CYAN}> Stopping services${NOCOLOR}"
# disable Nvidia tools that can auto load driver again
touch /run/hive/NV_OFF


modules=()
for (( i=0; i < try_count; i++ ))
do
	modules=()
	autoswitch stop > /dev/null 2>&1
	nvidia-oc stop > /dev/null 2>&1
	#wd stop > /dev/null 2>&1
	autofan stop > /dev/null 2>&1
	hivex stop >/dev/null 2>&1
	killall xinit > /dev/null 2>&1 && sleep 0.5
	killall nvidia-persistenced > /dev/null 2>&1 && sleep 0.5
	for mod in nvidia_drm nvidia_uvm nvidia_modeset nvidia;do
		if lsmod | grep -q $mod; then
			rmmod -f $mod > /dev/null 2>&1 && sleep 0.5 || modules+=($mod)
		fi
	done
	count_nvidia=`lsmod | grep -c nvidia`
	if [[ $count_nvidia -eq 0 ]]; then
		echo -e "${GREEN}> Unload modules successfull${NOCOLOR}"
		exit 0
	fi
	sleep 0.5
done

echo -e "${RED}> Unload modules failed (${modules[*]})${NOCOLOR}"
exit 1
