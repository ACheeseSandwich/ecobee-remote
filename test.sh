#!/bin/bash
#copy latest auth tokens from running code. 
#access token is only good for 1 hour
#after 1 hour app can regen it with a valid refresh code, if the refresh code is also stale then would need to regen all the keys from ecobee site. 
cp ~/run/ecobee-remote/ecobee_config.json .
python ./ecobee-remote.py --no-stats --check-only
