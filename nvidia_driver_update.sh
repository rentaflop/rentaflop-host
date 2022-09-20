#!/usr/bin/env bash
### Update Nvidia driver
### It will try to get the latest stable version from Hive server

. colors

[[ -e /etc/brand.conf ]] && source /etc/brand.conf
[[ -z $DOWNLOAD_URL ]] && DOWNLOAD_URL=http://download.hiveos.farm

DRVURL=$DOWNLOAD_URL/drivers/
DRVPATH=/hive-drivers-pack/
TIMEOUT=10
RETRY=3
MINFREEMB=700

DRVPATTERN="NVIDIA-Linux-x86_64-"
DRVREGEXP="${DRVPATTERN}\K[0-9\.]+(?=\.run)"
DRVNVIDIAURL="https://download.nvidia.com/XFree86/Linux-x86_64/"

# repo list
REPO_LIST=/etc/apt/sources.list.d/hiverepo.list

CUDA_VER=(
	11.2 460.27.04
	11.1 455.23.04
	11.0 450.51.05
	10.2 440.33
	10.1 418.39
	10.0 410.48
	9.2  396.26
	9.1  390.46
	9.0  384.81
	8.0  375.26
	7.5  352.31
	7.0  346.46
)

# in descending order!!!
KERNEL_SUPPORT=(
	5.10 "460"
	5.9  "460 455.45"
	5.8  "455 450.57 390.141"
	5.6  "450 440.82 390.138"
	5.4  "450 440.31 430.64 418.113 390.132"
	5.0  "418 410.104 390.116 340.108"
	4    ""
)


KERNEL_VER=
function is_kernel_supported() { # @driver_version
	local ver="$1"

	if [[ "${#KERNEL_VER[@]}" -lt 3 ]]; then
		# get kernel version as array
		readarray -t KERNEL_VER < <( uname -r | tr [:punct:] "\n")
		COMPAT_ARR=( ${KERNEL_SUPPORT[1]} )
		for (( i=0; i < ${#KERNEL_SUPPORT[@]}; i+=2 )) ; do
			readarray -t supp_ver < <(echo -e "${KERNEL_SUPPORT[$i]//./$'\n'}\n99")
			[[ ${KERNEL_VER[0]} -gt ${supp_ver[0]} ]] && break
			[[ ${KERNEL_VER[0]} -eq ${supp_ver[0]} && ${KERNEL_VER[1]} -gt ${supp_ver[1]} ]] && break
			COMPAT_ARR=( ${KERNEL_SUPPORT[$((i+1))]} )
		done
	fi

	readarray -t ver_arr < <(echo -e "${ver//./$'\n'}\n0\n0")
	# minimal version is 384.81 CUDA 9.0
	[[ "${ver_arr[0]}" -lt 384 || ( "${ver_arr[0]}" -eq 384 && "${ver_arr[1]#0}" -lt 81 ) ]] && return 2

	[[ -z "${COMPAT_ARR[@]}" ]] && return 0

	for ref in ${COMPAT_ARR[@]}; do
		[[ "$ref" == "$ver" ]] && return 0
		readarray -t ref_arr < <(echo -e "${ref//./$'\n'}\n0\n0")
		if [[ ${ver_arr[0]} -gt ${ref_arr[0]} ]]; then
			[[ ${ref_arr[1]#0} -eq 0 ]] && return 0
			return 1
		fi
		if [[ ${ver_arr[0]} -eq ${ref_arr[0]} ]]; then
			[[ ${ver_arr[1]#0} -gt ${ref_arr[1]#0} ]] && return 0
			[[ ${ver_arr[1]#0} -eq ${ref_arr[1]#0} && ${ver_arr[2]#0} -ge ${ref_arr[2]#0} ]] && return 0
		fi
	done

	return 1
}


function get_cuda_version() { # @driver_version, returns cuda_version
	local ver="$1"
	readarray -t ver_arr < <(echo -e "${ver//./$'\n'}\n0\n0")
	cuda_version=

	for (( i=1; i < ${#CUDA_VER[@]} ; i+=2 )); do
		readarray -t ref_arr < <(echo -e "${CUDA_VER[$i]//./$'\n'}\n0\n0")
		if [[ ${ver_arr[0]} -gt ${ref_arr[0]} ||
			( ${ver_arr[0]} -eq ${ref_arr[0]} && ${ver_arr[1]#0} -gt ${ref_arr[1]#0} ) ||
			( ${ver_arr[0]} -eq ${ref_arr[0]} && ${ver_arr[1]#0} -eq ${ref_arr[1]#0} && ${ver_arr[2]#0} -ge ${ref_arr[2]#0} ) ]]; then
			cuda_version=${CUDA_VER[$((i-1))]}
			return 0
		fi
	done

	return 1
}


function get_freespace() { # @silent
	local space=`df -k --output=avail $DRVPATH | tail -n 1 2>&1`
	[[ $space -ge $(( $MINFREEMB * 1024 )) ]] && return 0
	[[ -z "$1" ]] &&
		echo -e "" &&
		echo -e "${YELLOW}> Free space is less than ${WHITE}${MINFREEMB} MB${YELLOW} ($(( $space/1024 )) MB)${NOCOLOR}"
	return 1
}


function free_space() { # @url for package to keep
	get_freespace && return 0
	# step 1. try disk-expand
	disk-expand -s
	get_freespace 1 && return 0
	# step 2. remove some packages
	echo -e "${YELLOW}> Removing old driver packages${NOCOLOR}"
	remove_packages "$url"
	get_freespace 1 && return 0
	# step 3. clean ${DRVPATH} completely
	if [[ ! -z ${DRVPATH} && ${#DRVPATH} -gt 2 && -n "$(ls -A ${DRVPATH})" ]]; then
		echo -e ""
		echo -e "${YELLOW}> Removing everything from ${DRVPATH}${NOCOLOR}"
		rm -v -r --one-file-system ${DRVPATH}*
	fi
	get_freespace
	return $?
}


versions=()
function get_versions() { # returns $versions
	[[ ${#versions[@]} -gt 0 ]] && return 0
	echo -ne "${CYAN}> Loading drivers list - ${NOCOLOR}"
	local list=
	if [[ "$DRVURL" == "$DRVNVIDIAURL" ]]; then
		list=`curl -sLk --connect-timeout $TIMEOUT --retry $RETRY $DRVNVIDIAURL` &&
			readarray -t versions < <(echo "$list" | grep -oP "\>\K[0-9]+\.[0-9\.]+" | sort -u -V)
	else
		list=`curl -sLk --connect-timeout $TIMEOUT --retry $RETRY $DRVURL` &&
			readarray -t versions < <(echo "$list" | grep -oP "$DRVREGEXP" | sort -u -V)
	fi
	[[ ${#versions[@]} -eq 0 ]] && echo -e "${RED}Failed${NOCOLOR}" && return 1
	echo -e "${GREEN}${#versions[@]}${NOCOLOR}"
	return 0
}


function get_stable() { # returns $stable_version
	echo -en "${WHITE}> Stable version - ${NOCOLOR}"
	if [[ "$DRVURL" == "$DRVNVIDIAURL" ]]; then
		local list=`curl -sLk --connect-timeout $TIMEOUT --retry $RETRY ${DRVNVIDIAURL}latest.txt`
	else
		local list=`curl -sLk --connect-timeout $TIMEOUT --retry $RETRY ${DRVURL}VERSIONS.txt`
	fi
	[[ -z "$list" ]] && echo -e "${RED}Failed${NOCOLOR}" && return 1
	stable_version=`echo "$list" | grep -oP "$DRVREGEXP" | tail -n 1`
	[[ -z "$stable_version" ]] && echo -e "${RED}Error${NOCOLOR}" && return 2
	get_cuda_version "$stable_version" &&
		echo -e "${WHITE}${stable_version} ${PURPLE}(CUDA $cuda_version)${NOCOLOR}" ||
		echo -e "${WHITE}${stable_version}${NOCOLOR}"
	return 0
}


function get_current() { # @silent, returns $current_version
	current_version=`nvidia-smi --help 2>&1 | grep -m 1 -oP "v\K[0-9\.]+"`
	if [[ $? -ne 0 ]]; then
		[[ -z "$1" ]] && echo -e "${RED}> Installed version - UNKNOWN${NOCOLOR}"
		current_version=
		return 1
	fi
	if [[ -z "$1" ]]; then
		get_cuda_version "$current_version" &&
			echo -e "${GREEN}> Installed version - ${BGREEN}$current_version ${PURPLE}(CUDA $cuda_version)${NOCOLOR}" ||
			echo -e "${GREEN}> Installed version - $current_version${NOCOLOR}"
	fi
	return 0
}


function get_latest() { # returns $latest_version
	latest_version=
	get_versions || return
	get_current 1
	latest_version="${versions[-1]}"
	if [[ "${latest_version%%.*}" -lt "${current_version%%.*}" ]]; then
		echo "${RED}> This version ${WHITE}${latest_version}${RED} is lower than installed ${BGREEN}${current_version}${NOCOLOR}"
		return 2
	fi
	echo -en "${WHITE}> Latest version - ${NOCOLOR}"
	get_cuda_version "$latest_version" &&
		echo -e "${WHITE}${latest_version} ${PURPLE}(CUDA $cuda_version)${NOCOLOR}" ||
		echo -e "${WHITE}${latest_version}${NOCOLOR}"
	return 0
}


function remove_packages() { # @filename to skip, returns $removed_packages
	local files
	readarray -t files < <(realpath ${DRVPATH}NVIDIA-Linux* | grep -v "*" | sort -V)
	local cnt=${#files[@]}
	echo -e ""
	echo -e "${CYAN}> Found driver packages - $cnt${NOCOLOR}"
	[[ $cnt -eq 0 ]] && return 0

	local skip=
	if [[ ! -z "$1" ]]; then
		skip=`basename "$1"`
		# skip only if it exists
		#[[ ! -f ${DRVPATH}$skip ]] && skip=
	fi

	# skip current version by default
	[[ -z "$skip" ]] && get_current && skip="${DRVPATTERN}${current_version}.run"

	removed_packages=0
	for drv in "${files[@]}"
	do
		local basename=`basename "$drv"`
		if [[ "$basename" == "$skip" ]]; then
			echo -e "${GREEN}> Skipping - ${WHITE}$basename${NOCOLOR}"
		else
			echo -e "${YELLOW}> Deleting - ${WHITE}$basename${NOCOLOR}"
			unlink "$drv"
			((removed_packages++))
		fi
	done
	return 0
}


function list_packages() {
	get_versions
	get_current 1
	if [[ $? -eq 0 ]]; then
		local last=
		local cuda=
		local incompat=
		[[ $force -ne 1 ]] && level=1 || level=2
		for drv in "${versions[@]}"
		do
			is_kernel_supported "$drv"
			incompat=$?
			[[ $incompat -ge $level ]] && continue
			get_cuda_version "$drv"
			if [[ "$cuda" != "$cuda_version" ]]; then
				[[ ! -z $cuda ]] && echo ""
				cuda="$cuda_version"
				echo -e "${PURPLE}CUDA $cuda${NOCOLOR}"
				last=
			fi
			this="${drv%%.*}"
			if [[ "$last" != "$this" || -z "$last" ]]; then
				[[ ! -z "$last" ]] && echo -e ""
				last="$this"
				echo -ne "  ${WHITE}$this${NOCOLOR}"
			fi

			if [[ "$current_version" == "$drv" ]]; then
				echo -ne "	${BGREEN}$drv${NOCOLOR}"
			elif [[ $incompat -ne 0 ]]; then
				echo -ne "	${RED}$drv${NOCOLOR}"
			elif [[ -f "${DRVPATH}${DRVPATTERN}${drv}.run" ]]; then
				echo -ne "	${CYAN}$drv${NOCOLOR}"
			else
				echo -ne "	$drv"
			fi
		done
		echo ""
	fi

	get_stable
	get_current

	local files
	readarray -t files < <(realpath ${DRVPATH}NVIDIA-Linux* | grep -oP "$DRVREGEXP" | sort -V)
	[[ ${#files[@]} -eq 0 ]] && return 0
	echo -en "${CYAN}> Downloaded packages -"
	for drv in "${files[@]}"
	do
		local basename=`basename "$drv"`
		echo -en " $basename "
	done
	echo -e "${NOCOLOR}"
	[[ $force -eq 0 && "${#KERNEL_VER[@]}" -lt 3 && ${KERNEL_VER[0]} -ge 5 ]] &&
		echo -e "${YELLOW}> Kernel ${BYELLOW}${KERNEL_VER[0]}.${KERNEL_VER[1]}${YELLOW} compatible versions are shown (use ${BYELLOW}--force${YELLOW} to override)${NOCOLOR}"
}


function check_package() { # @filename
	#local basename=`basename $1`
	[[ ! -f "${DRVPATH}$1" ]] && return 1
	#echo -e ""
	echo -e "${CYAN}> Checking package integrity${NOCOLOR}"
	local exitcode=1
	# check size. zero file exits with 0
	local size=`stat -c %s "${DRVPATH}$1"`
	if [[ $size -gt 1000 ]]; then
		chmod +x "${DRVPATH}$1"
		"${DRVPATH}$1" --check
		exitcode=$?
	fi
	[[ $exitcode -ne 0 ]] && echo -e "${RED}> Check failed${NOCOLOR}"
	return $exitcode
}


function get_url() { # @version or @url, returns $url and $url_tesla
	url_tesla=
	# latest stable
	if [[ -z "$1" ]]; then
		get_latest || return $?
		url="${DRVURL}${DRVPATTERN}${latest_version}.run"
	# 440.95.01 & 123.45 formats
	elif [[ "$1" =~ ^[0-9]{3}\.[0-9]{2,3}\.[0-9]{2}$ || "$1" =~ ^[0-9]{3}\.[0-9]{2,3}$ ]]; then
		local last=
		get_versions
		if [[ $? -eq 0 ]]; then
			for drv in "${versions[@]}"
			do
				[[ "$drv" == "$1" || "${drv%.*}" == "$1" ]] && last="$drv" && break
			done
		fi
		if [[ ! -z "$last" ]]; then
			url="${DRVURL}${DRVPATTERN}${last}.run"
		else
			echo -e "${YELLOW}> ${WHITE}$1${YELLOW} was not found in the list. Trying to get it from NVIDIA${NOCOLOR}"
			url="${DRVNVIDIAURL}$1/${DRVPATTERN}$1.run"
			[[ "$1" =~ ^[0-9]{3}\.[0-9]{2,3}\.[0-9]{2}$ ]] &&
				url_tesla="https://uk.download.nvidia.com/tesla/$1/${DRVPATTERN}$1.run"
		fi
	# 123 format
	elif [[ "$1" =~ ^[0-9]{3}$ ]]; then
		get_versions || return $?
		local last=
		for drv in "${versions[@]}"
		do
			[[ "${drv%%.*}" == "$1" ]] && last="$drv" && continue
			[[ ! -z "$last" ]] && break
		done
		[[ -z "$last" ]] && echo -e "${RED}> Unable to find latest driver version for $1 series${NOCOLOR}" && return 1
		echo -e "${GREEN}> Latest driver for $1 series - ${WHITE}$last${NOCOLOR}"
		url="${DRVURL}${DRVPATTERN}${last}.run"
	# url
	else
		url="$1"
	fi
	[[ -z "$url" ]] && return 1
	return 0
}


function get_package() { # @url or @file, returns $package
	local exitcode=0
	local url="$1"

	package=`basename "$url"`
	[[ -z "$package" ]] && echo -e "${RED}> No file name in $url${NOCOLOR}" && return 1

	# check if file already exists and it is good
	local exist=0
	if [[ -f "${DRVPATH}$package" ]]; then
		echo -e ""
		echo -e "${YELLOW}> Driver package already exists${NOCOLOR}"
		check_package "$package" && return 0
		exist=1
	fi

	# local file
	if [[ "$url" != ftp* && "$url" != http* ]]; then
		#[[ ! -f $url ]] &&  echo -e "${RED} Unable to get from $url" && return 1
		realpath=`realpath "$url"`
		[[ "$realpath" == "${DRVPATH}$package" ]] && return 1
		cp "$url" "${DRVPATH}$package"
		[[ $? -ne 0 ]] && echo -e "${RED}> Unable to get file from - ${WHITE}$url${NOCOLOR}" && return 1
		check_package "$package"
		return $?
	fi

	for i in {1..2}; do
		# download file. resume if exists
		echo -e ""
		echo -e "${CYAN}> Downloading - ${WHITE}$url${NOCOLOR}"
		[ ! -t 1 ] && verb="-nv" # reduce log in non-interactive mode
		wget $verb --no-check-certificate -T $TIMEOUT -t $RETRY -c -P ${DRVPATH} $url 2>&1
		exitcode=$?
		[[ $exitcode -ne 0 ]] && echo -e "${RED}> Download error ($exitcode)${NOCOLOR}" && return $exitcode

		# check it again
		check_package "$package" && return 0

		# if file existed before, delete it and try download again. it would help if it was already broken
		[[ $exist -eq 0 ]] && return 1
		echo -e ""
		echo -e "${YELLOW}> File is broken. Deleting it and downloading again${NOCOLOR}"
		unlink "${DRVPATH}$package"
		exist=0
	done
}


function install_nvs() { # @force_install
	local exitcode=0
	#nvs_version=`dpkg -s nvidia-settings 2>&1 | grep '^Version: ' | sed 's/Version: //'`
	local nvs_version=`nvidia-settings --version | grep version | awk '{print $3}'`
	# Install strictly  361.42
	if [[ "$nvs_version" != 361.42* || $1 -eq 1 ]]; then
		echo -e ""
		echo -e "${CYAN}> Reinstalling nvidia-settings (current $nvs_version)${NOCOLOR}"
		#apt remove -y --allow-change-held-packages --purge nvidia-settings
		#apt install -y nvidia-settings=361.42-0ubuntu1
		apt install -y --reinstall --allow-downgrades --allow-change-held-packages nvidia-settings=361.42-0ubuntu1
		exitcode=$?
		# run apt update only on demand
		if [[ $exitcode -ne 0 ]]; then
			apt update
			[[ $exitcode -eq 100 ]] && apt-get -f install
			#apt install -y nvidia-settings=361.42-0ubuntu1
			apt install -y --reinstall --allow-downgrades --allow-change-held-packages nvidia-settings=361.42-0ubuntu1
			exitcode=$?
		fi
		[[ $exitcode -ne 0 ]] &&
			echo -e "${RED}> Nvidia-settings reinstall failed ($exitcode)${NOCOLOR}" ||
			echo -e "${GREEN}> Nvidia-settings reinstall successful${NOCOLOR}"
	fi
	apt-mark hold nvidia-settings > /dev/null 2>&1
	return $exitcode
}


function cuda_sync() {
	gpu_count_nvidia=`gpu-detect NVIDIA`
	CUDA_LIBS=( "libcudart.so" "libnvrtc.so" "libnvrtc-builtins.so" )
	if [[ $gpu_count_nvidia -gt 0 ]]; then
		cuda_version=
		get_current 1 && get_cuda_version "$current_version"
		CUDA_VERS="$cuda_version"
		if [[ ! -z "$CUDA_VERS" ]]; then
			for libname in "${CUDA_LIBS[@]}"; do
				[[ ! -f /hive/lib/${libname}.${CUDA_VERS} ]] && echo -e "${RED}> Error: ${libname}.${CUDA_VERS} does not exist${NOCOLOR}" && continue
				[[ ! -e "hive/lib/$libname" || "$(realpath /hive/lib/$libname)" != "$(realpath /hive/lib/${libname}.${CUDA_VERS})" ]] &&
					ln -f -s $(realpath /hive/lib/${libname}.${CUDA_VERS}) /hive/lib/$libname
			done
		fi
	fi
}


function install_driver() { # @url or @file, @force_install
	# it must exist
	[[ ! -d ${DRVPATH} ]] && mkdir ${DRVPATH}

	get_url "$1" || return $?

	# check compatibility
	local ver=`echo "$url" | grep -oP "${DRVREGEXP}"`
	if [[ ! -z "$ver" ]]; then
		is_kernel_supported "$ver"
		if [[ $? -ne 0 ]]; then
			echo "${RED}> WARNING: this version is incompatible with kernel ${BRED}${KERNEL_VER[0]}.${KERNEL_VER[1]}${NOCOLOR}"
			if [[ $force -ne 1 ]]; then
				echo "${YELLOW}> Use ${BYELLOW}--force${YELLOW} to override on your own risk${NOCOLOR}" &&
				return 1
			fi
		fi
		if [[ $force -ne 1 ]]; then
			get_current
			if [[ "$current_version" == "$ver" ]]; then
				echo "${YELLOW}> WARNING: this version is already installed (use ${BYELLOW}--force${YELLOW} to override)${NOCOLOR}" &&
				return 1
			fi
		fi
	fi

	# check avaliable space and try to get some
	free_space "$url"
	[[ $? -ne 0 ]] && echo -e "${RED}> Not enough free space to continue${NOCOLOR}" && return 1

	if [[ ! -z "$url_tesla" ]]; then
		get_package "$url" || get_package "$url_tesla" || return $?
	else
		get_package "$url" || return $?
	fi

	#cd $DRVPATH
	export TMPDIR=$DRVPATH
	local basename=`basename $package`
	# this check is redundant
	[[ ! -f "${DRVPATH}$basename" ]] && echo -e "${RED}> $basename not found${NOCOLOR}" && return 1
	#check_package "$basename"

	screen -wipe > /dev/null 2>&1
	sleep 1
	local as=$(screen -ls | grep -c autoswitch)
	local mn=$(screen -ls | grep -c miner)
	local exitcode=

	./nvstop.sh
	exitcode=$?
	if [[ $exitcode -eq 0 ]]; then
		local cmdline="--accept-license --no-questions --ui=none --dkms --no-install-compat32-libs" #--tmpdir=/hive-drivers-pack
		# ignore GCC version mismatch
		[[ $2 -eq 1 ]] && cmdline="$cmdline --no-cc-version-check" && local mode=" (forced mode)"
		echo -e ""
		echo -e "${CYAN}> Installing driver${mode}. ${WHITE}PLEASE WAIT!${NOCOLOR}"
		${DRVPATH}$basename $cmdline
		exitcode=$?
		# do not report error on driver loading without GPU
		[[ $exitcode -eq 1 && `gpu-detect NVIDIA` -eq 0 ]] &&
			grep -q "Unable to load the 'nvidia-drm' kernel module" /var/log/nvidia-installer.log &&
			exitcode=0
		if [[ $exitcode -eq 0 ]]; then
			echo -e "${GREEN}> Done${NOCOLOR}"
		else
			echo -e "${YELLOW}> Failed ($exitcode)${NOCOLOR}"
		fi
	fi

	install_nvs || exitcode=$?

	echo -e ""
	nvstop start

	[[ $mn -ne 0 ]] && miner start
	[[ $as -ne 0 ]] && nohup bash -c 'sleep 15 && autoswitch start' > /tmp/nohup.log 2>&1 &

	[[ $exitcode -ne 0 ]] && echo -e "${RED}> Driver installation failed ($exitcode)${NOCOLOR}" && return 1

	echo -e "${GREEN}> Driver installation successful${NOCOLOR}" # ${WHITE}REBOOT NOW!${NOCOLOR}

	# send new driver version
	cuda_sync
	hello redetect > /dev/null 2>&1
	return 0
}


function select_package() {
	while true;
	do
		read -p "${BYELLOW}> Enter the version to install: ${NOCOLOR}" REPLY
		[[ "$REPLY" == "" || "$REPLY" == "0" ]] && return 1
		[[ "$REPLY" =~ ^([0-9]{3}|[0-9]{3}\.[0-9]{2,3}|[0-9]{3}\.[0-9]{2,3}\.[0-9]{2})$ ]] && break
		echo -e "${RED}Invalid selection$NOCOLOR"
	done
	driver="$REPLY"
	return 0
}


function show_help() {
	echo "Usage:
  nvidia-driver-update  		download and install latest driver version
  nvidia-driver-update  URL		download and install driver from URL (http/https/ftp)
  nvidia-driver-update  123		download and install latest driver from series 123.*
  nvidia-driver-update  123.45.06	download and install driver version 123.45.06
  nvidia-driver-update  -s | --stable	download and install STABLE driver version
  nvidia-driver-update  -f | --force	force install to bypass some DKMS build errors and etc
  nvidia-driver-update  -l | --list	list available driver versions (compatible with current kernel)
  nvidia-driver-update  -e | --ext	use external drivers repository
  nvidia-driver-update  -c | --cuda	sync symlinks to CUDA RT libraries with installed driver
  nvidia-driver-update  -n | --nvs	reinstall nvidia-settings only
  nvidia-driver-update  -r | --remove	remove downloaded driver packages except currently installed
  nvidia-driver-update  -h | --help	display help
  nvidia-driver-update  --repo[=URL]	use custom repository (specified by URL) (HTTP(S)/FTP)
Examples:
  nvidia-driver-update --force 455
  nvidia-driver-update --force --ext --list
  nvidia-driver-update --list --repo=ftp://192.168.1.1/drivers/
"
}


driver=
force=

for param in $@; do
	case "$param" in

		-e|--ext)
			DRVURL=$DRVNVIDIAURL
		;;

		-f|--force)
			force=1
		;;

		--repo)
			[[ ! -f $REPO_LIST ]] && echo -e "${RED}> No default repository, exiting${NOCOLOR}" && exit 1
			DRVURL=`grep -m1 -oP "deb\s*\K[^\s]+/repo/" $REPO_LIST`
			[[ -z "$DRVURL" || "$DRVURL" =~ "${DOWNLOAD_URL##*/}" ]] && echo -e "${RED}> No custom repository, exiting${NOCOLOR}" && exit 1
			[[ ! "$DRVURL" =~ /$ ]] && DRVURL+="/"
		;;

		--repo=*)
			[[ -z ${param#*=} ]] && echo -e "${RED}> Repository URL is required, exiting${NOCOLOR}" && exit 1
			DRVURL="${param#*=}"
			[[ ! "$DRVURL" =~ /$ ]] && DRVURL+="/"
		;;

	esac
done


for param in $@; do
	case "$param" in
		--help|-h)
			show_help
			exit
		;;

		-c|--cuda)
			cuda_sync
			exit
		;;

		-l|--list)
			list_packages
			[[ -t 0 ]] && select_package && break
			exit
		;;

		-n|--nvs)
			install_nvs 1
			exit
		;;

		-r|--remove)
			remove_packages
			exit
		;;

		-s|--stable)
			get_stable || exit
			driver="$stable_version"
		;;

		*)
			[[ "$param" == -* ]] && continue
			#[[ "$param" == -* ]] && echo -e "${YELLOW}> Unsupported option \"$1\"${NOCOLOR}" && exit 1
			#123
			#123.45
			#url/path
			driver="$param"
		;;
	esac
done

install_driver "$driver" "$force"

exit
