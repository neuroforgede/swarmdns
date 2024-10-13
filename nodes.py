#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2022 NeuroForge GmbH & Co. KG <https://neuroforge.de>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import socket
from dnslib import DNSRecord, DNSHeader, RR, A
import threading

import docker
import json
from datetime import datetime
from typing import Any
from threading import Event
import signal
from typing import Dict, List
import os
import boto3

exit_event = Event()

def handle_shutdown(signal: Any, frame: Any) -> None:
    print_timed(f"received signal {signal}. shutting down...")
    exit_event.set()

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


SCRAPE_INTERVAL = int(os.getenv('SCRAPE_INTERVAL', '10'))
MAX_RETRIES_IN_ROW = int(os.getenv('MAX_RETRIES_IN_ROW', '10'))
DEBUG = os.getenv('DEBUG', 'false').lower() == 'true'

def print_debug(msg):
    if DEBUG:
        print(msg)

def print_timed(msg):
    to_print = '{} [{}]: {}'.format(
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'docker_events',
        msg)
    print(to_print)

def get_nodes_from_docker():
    client = docker.from_env()
    nodes_response = client.nodes.list()

    nodes = []

    for node in nodes_response:
        node_info = node.attrs
        node_name = node_info['Description']['Hostname']
        node_ip = node_info['Status']['Addr']
        node_id = node_info['ID']
        nodes.append({
            'id': node_id,
            'name': node_name,
            'ip': node_ip
        })

    return {
        'nodes': nodes
    }


def save_json(data, filename):
    """Save the network data to a JSON file."""
    with open(filename, 'w') as f:
        json.dump(data, f, indent=4)
    print_timed(f"Network data saved to {filename}")


def upload_to_dns_s3(file_name, bucket, object_name=None):
    """Upload a file to Hetzner S3 bucket.

    :param file_name: File to upload
    :param bucket: Bucket to upload to
    :param object_name: S3 object name. If not specified then file_name is used
    :return: True if file was uploaded, else False
    """
    # Hetzner S3 configuration
    dns_s3_endpoint = os.environ['DNS_S3_ENDPOINT']
    hetzner_access_key = os.environ['HETZNER_ACCESS_KEY']
    hetzner_secret_key = os.environ['HETZNER_SECRET_KEY']

    # Create the S3 client with custom endpoint for Hetzner
    s3_client = boto3.client('s3',
                             endpoint_url=dns_s3_endpoint,
                             aws_access_key_id=hetzner_access_key,
                             aws_secret_access_key=hetzner_secret_key)

    try:
        if object_name is None:
            object_name = file_name
        s3_client.upload_file(file_name, bucket, object_name)
        print_timed(f"File {file_name} uploaded to {bucket}/{object_name} on Hetzner S3")
    except Exception as e:
        print_timed(f"Error uploading {file_name} to Hetzner S3: {e}")
        return False
    return True


if __name__ == '__main__':
    while not exit_event.is_set():
        print('Fetching nodes from Docker...')
        nodes_info = get_nodes_from_docker()

        filename = 'nodes.json'

        save_json(nodes_info, filename)
        upload_to_dns_s3(filename, os.environ['DNS_S3_BUCKET_NAME'])

        exit_event.wait(SCRAPE_INTERVAL)