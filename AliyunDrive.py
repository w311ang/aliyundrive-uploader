# -*- coding: utf-8 -*-
# +-------------------------------------------------------------------
# | 阿里云盘上传类
# +-------------------------------------------------------------------
# | Author: 李小恩 <i@abcyun.cc>
# +-------------------------------------------------------------------
import json
import math
import os
import time
from hashlib import sha1

import requests
from tqdm import tqdm

import common

requests.packages.urllib3.disable_warnings()
from common import print_warn, print_info, print_error, print_success, get_hash


class AliyunDrive:
    def __init__(self, drive_id, root_path, refresh_token, chunk_size=10485760):
        self.start_time = time.time()
        self.drive_id = drive_id
        self.root_path = root_path
        self.refresh_token = refresh_token
        self.chunk_size = chunk_size
        self.filepath = None
        self.filepath_hash = None
        self.realpath = None
        self.filename = None
        self.hash = None
        self.part_info_list = []
        self.part_upload_url_list = []
        self.upload_id = 0
        self.file_id = 0
        self.part_number = 0

    def load_file(self, filepath, realpath):
        self.filepath = filepath
        self.filepath_hash = sha1(filepath.encode('utf-8')).hexdigest()
        self.realpath = realpath
        self.filename = os.path.basename(realpath)
        print_info('【{filename}】正在校检文件中，耗时与文件大小有关'.format(filename=self.filename))
        self.hash = get_hash(self.realpath)
        self.filesize = os.path.getsize(self.realpath)

        self.part_info_list = []
        for i in range(0, math.ceil(self.filesize / self.chunk_size)):
            self.part_info_list.append({
                'part_number': i + 1
            })

        message = '''=================================================
        文件名：{filename}
        hash：{hash}
        文件大小：{filesize}
        文件路径：{filepath}
=================================================
'''.format(filename=self.filename, hash=self.hash, filesize=self.filesize, filepath=self.realpath)
        print_info(message)

    def token_refresh(self):
        common.LOCK.acquire()
        try:
            data = {"refresh_token": self.refresh_token}
            post = requests.post(
                'https://websv.aliyundrive.com/token/refresh',
                data=json.dumps(data),
                headers={
                    'content-type': 'application/json;charset=UTF-8'
                },
                verify=False
            )
            try:
                post_json = post.json()
                # 刷新配置中的token
                with open(os.path.dirname(os.path.realpath(__file__)) + '/config.json', 'rb') as f:
                    config = json.loads(f.read().decode('utf-8'))
                config['REFRESH_TOKEN'] = post_json['refresh_token']
                with open(os.path.dirname(os.path.realpath(__file__)) + '/config.json', 'w') as f:
                    f.write(json.dumps(config))
                    f.flush()

            except Exception as e:
                print_warn('refresh_token已经失效')
                raise e

            access_token = post_json['access_token']
            self.headers = {
                'authorization': access_token
            }
            self.refresh_token = post_json['refresh_token']
        finally:
            common.LOCK.release()

    def create(self, parent_file_id):
        create_data = {
            "drive_id": self.drive_id,
            "part_info_list": self.part_info_list,
            "parent_file_id": parent_file_id,
            "name": self.filename,
            "type": "file",
            "check_name_mode": "auto_rename",
            "size": self.filesize,
            "content_hash": self.hash,
            "content_hash_name": 'sha1'
        }
        create_post = requests.post(
            'https://api.aliyundrive.com/v2/file/create',
            data=json.dumps(create_data),
            headers=self.headers,
            verify=False
        )
        create_post_json = create_post.json()
        if create_post_json.get('code') == 'AccessTokenInvalid':
            print_info('AccessToken已失效，尝试刷新AccessToken中')
            if self.token_refresh():
                print_info('AccessToken刷新成功，返回创建上传任务')
                return self.create(parent_file_id)
            print_error('无法刷新AccessToken，准备退出')
            exit()

        self.part_upload_url_list = create_post_json.get('part_info_list')
        self.file_id = create_post_json.get('file_id')
        self.upload_id = create_post_json.get('upload_id')
        return create_post_json

    def get_upload_url(self):
        print_info('【%s】上传地址已失效正在获取新的上传地址' % self.filename)
        requests_data = {
            "drive_id": self.drive_id,
            "file_id": self.file_id,
            "part_info_list": self.part_info_list,
            "upload_id": self.upload_id,
        }
        requests_post = requests.post(
            'https://api.aliyundrive.com/v2/file/get_upload_url',
            data=json.dumps(requests_data),
            headers=self.headers,
            verify=False
        )
        requests_post_json = requests_post.json()
        if requests_post_json.get('code') == 'AccessTokenInvalid':
            print_info('AccessToken已失效，尝试刷新AccessToken中')
            if self.token_refresh():
                print_info('AccessToken刷新成功，返回创建上传任务')
                return self.get_upload_url()
            print_error('无法刷新AccessToken，准备退出')
            exit()
        print_info('【%s】上传地址刷新成功' % self.filename)
        return requests_post_json.get('part_info_list')

    def upload(self):

        with open(self.realpath, "rb") as f:
            with tqdm.wrapattr(f, "read", desc='正在上传【%s】' % self.filename, miniters=1,
                               initial=self.part_number * self.chunk_size,
                               total=self.filesize,
                               ascii=True
                               ) as fs:

                while self.part_number < len(self.part_upload_url_list):
                    upload_url = self.part_upload_url_list[self.part_number]['upload_url']
                    total_size = min(self.chunk_size, self.filesize)
                    fs.seek(self.part_number * total_size)
                    res = requests.put(
                        url=upload_url,
                        data=common.read_in_chunks(fs, 16 * 1024, total_size),
                        verify=False,
                        timeout=None
                    )
                    if 400 <= res.status_code < 600:
                        common_get_xml_value = common.get_xml_tag_value(res.text, 'Message')
                        if common_get_xml_value == 'Request has expired.':
                            self.part_upload_url_list = self.get_upload_url()
                            continue
                        common_get_xml_value = common.get_xml_tag_value(res.text, 'Code')
                        if common_get_xml_value == 'PartAlreadyExist':
                            pass
                        else:
                            print_error(res.text)
                            res.raise_for_status()
                    self.part_number += 1
                    common.DATA['tasks'][self.filepath_hash]['part_number'] = self.part_number
                    common.DATA['tasks'][self.filepath_hash]['drive_id'] = self.drive_id
                    common.DATA['tasks'][self.filepath_hash]['file_id'] = self.file_id
                    common.DATA['tasks'][self.filepath_hash]['upload_id'] = self.upload_id
                    common.DATA['tasks'][self.filepath_hash]['chunk_size'] = self.chunk_size
                    common.save_task(common.DATA['tasks'])

    def complete(self):
        complete_data = {
            "drive_id": self.drive_id,
            "file_id": self.file_id,
            "upload_id": self.upload_id
        }
        complete_post = requests.post(
            'https://api.aliyundrive.com/v2/file/complete', json.dumps(complete_data),
            headers=self.headers,
            verify=False
        )

        complete_post_json = complete_post.json()
        if complete_post_json.get('code') == 'AccessTokenInvalid':
            print_info('AccessToken已失效，尝试刷新AccessToken中')
            if self.token_refresh():
                print_info('AccessToken刷新成功，返回创建上传任务')
                return self.complete()
            print_error('无法刷新AccessToken，准备退出')
            exit()
        s = time.time() - self.start_time
        if 'file_id' in complete_post_json:
            print_success('【{filename}】上传成功！消耗{s}秒'.format(filename=self.filename, s=s))
            return True
        else:
            print_warn('【{filename}】上传失败！消耗{s}秒'.format(filename=self.filename, s=s))
            return False

    def create_folder(self, folder_name, parent_folder_id):
        create_data = {
            "drive_id": self.drive_id,
            "parent_file_id": parent_folder_id,
            "name": folder_name,
            "check_name_mode": "refuse",
            "type": "folder"
        }
        create_post = requests.post(
            'https://api.aliyundrive.com/v2/file/create',
            data=json.dumps(create_data),
            headers=self.headers,
            verify=False
        )
        create_post_json = create_post.json()
        if create_post_json.get('code') == 'AccessTokenInvalid':
            print_info('AccessToken已失效，尝试刷新AccessToken中')
            if self.token_refresh():
                print_info('AccessToken刷新成功，返回创建上传任务')
                return self.create_folder(folder_name, parent_folder_id)
            print_error('无法刷新AccessToken，准备退出')
            exit()
        return create_post_json.get('file_id')

    def get_parent_folder_id(self, filepath):
        print_info('检索目录中')
        filepath_split = (self.root_path + filepath.lstrip(os.sep)).split(os.sep)
        del filepath_split[len(filepath_split) - 1]
        path_name = os.sep.join(filepath_split)
        if not path_name in common.DATA['folder_id_dict']:
            parent_folder_id = 'root'
            parent_folder_name = os.sep
            if len(filepath_split) > 0:
                for folder in filepath_split:
                    if folder == '':
                        continue
                    parent_folder_id = self.create_folder(folder, parent_folder_id)
                    parent_folder_name = parent_folder_name.rstrip(os.sep) + os.sep + folder
                    common.DATA['folder_id_dict'][parent_folder_name] = parent_folder_id
        else:
            parent_folder_id = common.DATA['folder_id_dict'][path_name]
            print_info('已存在目录，无需创建')

        print_info('目录id获取成功{parent_folder_id}'.format(parent_folder_id=parent_folder_id))
        return parent_folder_id
