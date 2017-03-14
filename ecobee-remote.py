from temperusb import TemperHandler
import logging
from pyecobee import Ecobee
import json
from datetime import datetime
import time
from time import gmtime, strftime
import calendar
from influxdb import InfluxDBClient
from influxdb.client import InfluxDBClientError
import argparse
import ConfigParser

#logging.basicConfig(filename='example.log',level=logging.DEBUG)

def getHvacMode(thermostat):
    return thermostat['settings']['hvacMode']

def getCurrentTemp(thermostat):
    return thermostat['runtime']['actualTemperature'] / float(10)

def getHeatSetPoint(thermostat):
    return thermostat['runtime']['desiredHeat']  / float(10)

def getCoolSetPoint(thermostat):
    return thermostat['runtime']['desiredCool'] / float(10)

def isEquipmentRunning(thermostat):
    return len(thermostat['equipmentStatus']) > 0

def log_short_status(thermostat):
    logging.info("Hvac Mode: %s" % getHvacMode(thermostat))
    logging.info("Current Temp: %s" % getCurrentTemp(thermostat))
    logging.info("Heat Set Point: %s" % getHeatSetPoint(thermostat))
    logging.info("Cool Set Point: %s" % getCoolSetPoint(thermostat))
    logging.info("Equipment running: %s" % isEquipmentRunning(thermostat))

#influx metrics format
#[(key, value)]
#key = {
# measurement: 'realmetricname',
# tag1: 'tagvalue',
# tag2: 'tagvalue'
#}
#
def publish_to_influx(host, port, db, poll_time, results):
    logging.info("Creating influx client to %s:%s db:%s" % (host, port, db))
    client = InfluxDBClient(host, port, database=db)
    #influxdb requires timestamps in NANOSECONDS
    ts = calendar.timegm(poll_time) * 1000000000

    series = []
    for r in results:
        if len(r) != 2:
            logging.warn("skipping bad measurement %s" % r)
            continue
        point = {
            "time": ts,
            "measurement": r[0]['measurement'],
            "fields": {
                "value": r[1]
            },
            "tags": {
                
            }
        }
        for k in r[0].keys():
            if k != 'measurement':
                point['tags'][k] = r[0][k]

        series.append(point)
        
    logging.info("publishing %s series " % len(series))
    client.write_points(series)
    logging.info("publishing complete")

def extract_thermostat_metrics(thermostat,metrics):
    metrics.append(({
        'measurement': 'TemperatureF',
        'location': 'thermostat'
    },getCurrentTemp(thermostat)))
    
    metrics.append(({
        'measurement': 'SetPointF',
        'operation': 'heat',
    },getHeatSetPoint(thermostat)))

    equipment_status = 0
    if isEquipmentRunning(thermostat):
        equipment_status = 1

    metrics.append(({
        'measurement': 'EquipmentStatus',
        'equipment': 'furnace'
    },equipment_status))

def fetch_room_temperature(location,metrics):
    th = TemperHandler()
    devs = th.get_devices()
    thermometer = devs[0]
    temp = thermometer.get_temperature(format='fahrenheit')
    logging.info("Temp in %s is %s Deg F" % (location,temp))
    
    #build a metric
    key = {
        'measurement': 'TemperatureF',
        'location': location
        }
    metric = (key,temp)
    metrics.append(metric)
    return temp

def verify_hold_set(ecobee, heat_hold, max_wait_sec):
    thermostats = None
    for i in range(0,max_wait_sec):
        thermostats = ecobee.get_thermostats()
        if getHeatSetPoint(thermostats[0]) == heat_hold:
            logging.info("SUCCESS! verified heat hold updated to %s" % heat_hold)
            #return thermostats so we can publish metrics on most recent status
            return thermostats
        time.sleep(1)
    logging.critical("FAILURE: waited %s sec and set point was not updated")
    return thermostats

def dump_metrics(metrics):
    for metric in metrics:
        print metric

def dump_config(config):
    print "location: %s" % config.get('main','location')
    print "heat_on_threshold: %s" % config.getint('main','heat_on_threshold')
    print "heat_off_threshold: %s" % config.getint('main','heat_off_threshold')
    print "heat_on_setpoint: %s" % config.getint('main', 'heat_on_setpoint')
    print "heat_off_setpoint: %s" % config.getint('main', 'heat_off_setpoint')
    print "influx host: %s" % config.get('db', 'influx_host')
    print "influx port: %s" % config.getint('db', 'influx_port')
    print "influx_dbname: %s" % config.get('db', 'influx_dbname')

    

if __name__ == '__main__':

#init
    parser = argparse.ArgumentParser(description='Ecobee remote control')
    parser.add_argument('--check-only', dest='adjust_temp', action='store_const', const=False, default=True)
    parser.add_argument('--no-stats', dest='publish_stats', action='store_const', const=False, default=True)
    parser.add_argument('--max-wait-sec', dest='max_wait_sec', type=int, default=60, help='max sec to wait to verify set request to ecobee went through')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p %Z')
    log = logging.getLogger('main')
    log.info("Starting up...")

    config = ConfigParser.RawConfigParser()
    config.read('ecobee-remote.conf')
    
    location = config.get('main', 'location')
    heat_on_threshold = config.getint('main', 'heat_on_threshold')
    heat_off_threshold = config.getint('main', 'heat_off_threshold')
    heat_on_setpoint = config.getint('main', 'heat_on_setpoint')
    heat_off_setpoint = config.getint('main', 'heat_off_setpoint')
    influx_host = config.get('db', 'influx_host')
    influx_port = config.getint('db', 'influx_port')
    influx_dbname = config.get('db', 'influx_dbname')

    poll_time = gmtime()

    metrics = []

    #poll room temp
    temp = fetch_room_temperature(location,metrics)

    #fetch thermostat info once
    ecobee = Ecobee(config_filename='ecobee_config.json')
    ecobee.write_tokens_to_file()
    thermostats = ecobee.get_thermostats()
    log_short_status(thermostats[0])

    current_heat_setpoint = getHeatSetPoint(thermostats[0])

    if not args.adjust_temp:
        log.info("DRY RUN MODE. CHECKING TEMP ONLY")

    if current_heat_setpoint < heat_off_setpoint or current_heat_setpoint > heat_on_setpoint:
        log.warning("OVERRIDE DETECTED: Current thermostat setpoint of %s is outside program range of %s to %s. Not adjusting temperature." % (current_heat_setpoint, heat_off_setpoint, heat_on_setpoint))
        args.adjust_temp = False

    if heat_on_threshold >= heat_off_threshold:
        log.critical("Heat on threshold of %s is >= heat off threshold of %s. Will not adjust temp" % (heat_on_threshold, heat_off_threshold))
        args.adjust_temp = False


    #if adjust
    #  set hold
    #  verify hold set (refreshes thermostat info)
    new_setpoint = None
    if temp <= heat_on_threshold:
        new_setpoint = heat_on_setpoint
        log.info("HEAT ON: %s temp of %s is below %s. setting hold to %s" % (location, temp, heat_on_threshold, new_setpoint))

    elif temp >= heat_off_threshold:
        new_setpoint = heat_off_setpoint
        log.info("HEAT OFF: %s temp of %s is above %s. setting hold to %s" % (location, temp, heat_off_threshold, new_setpoint))

    if args.adjust_temp and new_setpoint:
        if new_setpoint != current_heat_setpoint:
            ecobee.set_hold_temp(0,new_setpoint,new_setpoint, hold_type="indefinite")
            #save results of last poll in thermostats so we publish most recent values
            thermostats = verify_hold_set(ecobee, new_setpoint, args.max_wait_sec)
        else:
            log.info("Heat already set to %s, skipping" % new_setpoint)


    extract_thermostat_metrics(thermostats[0], metrics)
    dump_metrics(metrics)

    #publish metrics
    if args.publish_stats:
        publish_to_influx(influx_host, influx_port, influx_dbname, poll_time, metrics)
    else:
        log.info("Skipping metrics --no-stats set")
