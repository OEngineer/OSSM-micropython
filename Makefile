install: install_dependencies install_firmware

install_firmware:
	# Copy firmware
	mpremote cp -r src/ :
	mpremote cp main.py :

install_dependencies:
	# Install dependencies
	mpremote mip install github:bikeNomad/micropython-rp2-smartStepper
	mpremote mip install aioble
	mpremote mip install github:peterhinch/micropython-async/v3/primitives
	mpremote mip install aiorepl
	

# vim: ts=4 sw=4 noet
