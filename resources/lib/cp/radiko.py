# -*- coding: utf-8 -*-

from jcba import Jcba

from ..const import Const
from ..common import *
from ..xmltodict import parse

import os
import struct
import zlib
import urllib2
import xbmc, xbmcgui, xbmcplugin, xbmcaddon

from base64 import b64encode
from math import ceil


class Params:
    # ファイルパス
    DATA_PATH = os.path.join(Const.DATA_PATH, 'radiko')
    if not os.path.isdir(DATA_PATH): os.makedirs(DATA_PATH)
    # ファイル
    PROGRAM_FILE  = os.path.join(DATA_PATH, 'program.xml')
    STATION_FILE  = os.path.join(DATA_PATH, 'station.json')
    SETTINGS_FILE = os.path.join(DATA_PATH, 'settings.xml')
    NEXTUPDT_FILE = os.path.join(DATA_PATH, 'nextupdt.json')
    # URL
    STATION_URL   = 'http://radiko.jp/v2/station/list/%s.xml'
    REFERER_URL   = 'http://radiko.jp/player/timetable.html'
    PROGRAM_URL   = 'http://radiko.jp/v2/api/program/now?area_id=%s'
    STREAM_URL    = 'rtmpe://f-radiko.smartstream.ne.jp'
    # 遅延
    DELAY         = 3


class Authenticate:

    # radikoのプレーヤ(player.swf)をダウンロード
    # player.swfに潜むRadikoPlayer_keyImageを抽出
    # https://radiko.jp/v2/api/auth1_fmsへPOSTでアクセスしてauthtokenとKeyLength、KeyOffsetを取得
    # KeyLength、KeyOffsetを基にRadikoPlayer_keyImageからバイナリデータを取得しBASE64で符号化(PartialKey)
    # authtokenとPartialKeyをリクエストヘッダに載せてhttps://radiko.jp/v2/api/auth2_fmsへPOSTでアクセス
    # 認証に成功すればauth_tokenを使ってrtmpdumpでデータを受信

    # cf. http://d.hatena.ne.jp/zariganitosh/20130124/rtmpdump_radiko_access

    # ファイル
    KEY_FILE    = os.path.join(Params.DATA_PATH, 'authkey.dat')
    PLAYER_FILE = os.path.join(Params.DATA_PATH, 'player.swf')
    # URL
    AUTH1_URL   = 'https://radiko.jp/v2/api/auth1_fms'
    AUTH2_URL   = 'https://radiko.jp/v2/api/auth2_fms'
    PLAYER_URL  = 'http://radiko.jp/apps/js/flash/myplayer-release.swf'
    # その他
    OBJECT_TAG  = 87
    OBJECT_ID   = 12

    def __init__(self, renew=True):
        # キーファイル作成
        if renew or not os.path.isfile(self.KEY_FILE):
            self.createKeyFile()
        # responseを初期化
        self.response = response = {'auth_token':'', 'area_id':'', 'authed':0}
        # auth_tokenを取得
        response = self.appIDAuth(response)
        if response and response['auth_token']:
            # area_idを取得
            response = self.challengeAuth(response)
            if response and response['area_id']:
                response['authed'] = 1
                # インスタンス変数に格納
                self.response = response
            else:
                log('challengeAuth failed.')
        else:
            log('appIDAuth failed.')

    # キーファイルを作成
    def createKeyFile(self):
        # PLAYER_URLのオブジェクトのサイズを取得
        try:
            response = urllib2.urlopen(self.PLAYER_URL)
            size = int(response.headers["content-length"])
        except Exception as e:
            log(str(e), error=True)
            return
        # PLAYERファイルのサイズと比較、異なっている場合はダウンロードしてKEYファイルを生成
        if not os.path.isfile(self.PLAYER_FILE) or size != int(os.path.getsize(self.PLAYER_FILE)):
            swf = response.read()
            with open(self.PLAYER_FILE, 'wb') as f:
                f.write(swf)
            # 読み込んだswfバッファ
            self.swf = swf[:8] + zlib.decompress(swf[8:])
            # swf読み込みポインタ
            self.pos = 0
            # ヘッダーパース
            self.__header()
            # タブブロックがある限り
            while self.__block():
                if self.block['tag'] == self.OBJECT_TAG and self.block['id'] == self.OBJECT_ID:
                    with open(self.KEY_FILE, 'wb') as f:
                        f.write(self.block['value'])
                    break

    # パーシャルキーを生成
    def createPartialKey(self, response):
        with open(self.KEY_FILE, 'rb') as f:
            f.seek(response['key_offset'])
            partialkey = b64encode(f.read(response['key_length'])).decode('utf-8')
        return partialkey

    # ヘッダーパース
    def __header(self):
        self.magic   = self.__read(3)
        self.version = ord(self.__read(1))
        self.file_length = self.__le4Byte(self.__read(4))
        rectbits = ord(self.__read(1)) >> 3
        total_bytes = int(ceil((5 + rectbits * 4) / 8.0))
        twips_waste = self.__read(total_bytes - 1)
        self.frame_rate_decimal = ord(self.__read(1))
        self.frame_rate_integer = ord(self.__read(1))
        self.frame_count = self.__le2Byte(self.__read(2))

    # ブロック判定
    def __block(self):
        blockStart = self.pos
        tag = self.__le2Byte(self.__read(2))
        blockLen = tag & 0x3f
        if blockLen == 0x3f:
            blockLen = self.__le4Byte(self.__read(4))
        tag = tag >> 6
        if tag == 0:
            return None
        else:
            self.blockPos = 0
            self.block = {
                'block_start': blockStart,
                'tag': tag,
                'block_len': blockLen,
                'id': self.__le2Byte(self.__read(2)),
                'alpha': self.__read(4),
                'value': self.__read(blockLen-6) or None
            }
            return True

    # ユーティリティ
    def __read(self, num):
        self.pos += num
        return self.swf[self.pos - num: self.pos]

    def __le2Byte(self, s):
        # LittleEndian to 2 Byte
        return struct.unpack('<H', s)[0]

    def __le4Byte(self, s):
        # LittleEndian to 4 Byte
        return struct.unpack('<L', s)[0]

    # auth_tokenを取得
    def appIDAuth(self, response):
        # ヘッダ
        headers = {
            'pragma': 'no-cache',
            'X-Radiko-App': 'pc_ts',
            'X-Radiko-App-Version': '4.0.0',
            'X-Radiko-User': 'test-stream',
            'X-Radiko-Device': 'pc'
        }
        try:
            # リクエスト
            req = urllib2.Request(self.AUTH1_URL, headers=headers, data='\r\n')
            # レスポンス
            auth1fms = urllib2.urlopen(req).info()
        except Exception as e:
            log(str(e), error=True)
            return
        response['auth_token'] = auth1fms['X-Radiko-AuthToken']
        response['key_offset'] = int(auth1fms['X-Radiko-KeyOffset'])
        response['key_length'] = int(auth1fms['X-Radiko-KeyLength'])
        return response

    # area_idを取得
    def challengeAuth(self, response):
        # ヘッダ
        response['partial_key'] = self.createPartialKey(response)
        headers = {
            'pragma': 'no-cache',
            'X-Radiko-App': 'pc_ts',
            'X-Radiko-App-Version': '4.0.0',
            'X-Radiko-User': 'test-stream',
            'X-Radiko-Device': 'pc',
            'X-Radiko-Authtoken': response['auth_token'],
            'X-Radiko-Partialkey': response['partial_key']
        }
        try:
            # リクエスト
            req = urllib2.Request(self.AUTH2_URL, headers=headers, data='\r\n')
            # レスポンス
            auth2fms = urllib2.urlopen(req).read().decode('utf-8')
        except Exception as e:
            log(str(e), error=True)
            return
        response['area_id'] = auth2fms.split(',')[0].strip()
        return response


class Radiko(Params, Jcba):

    def __init__(self, area, token, renew=False):
        self.area = area
        self.token = token
        # 放送局データと設定データを初期化
        if self.area and self.token:
            self.setup(renew)

    def setup(self, renew=False):
        # キャッシュがあれば何もしない
        if renew == False and os.path.isfile(self.STATION_FILE) and os.path.isfile(self.SETTINGS_FILE):
            return
        # キャッシュがなければウェブから読み込む
        data = urlread(self.STATION_URL % self.area)
        if data:
            # データ変換
            dom = convert(parse(data))
            station = dom['stations'].get('station',[]) if dom['stations'] else []
            station = station if isinstance(station,list) else [station]
            # 放送局データ
            buf = []
            for s in station:
                buf.append({
                    'id': 'radiko_%s' % s['id'],
                    'name': s['name'],
                    'url': s['href'],
                    'logo_large': s['logo_large'],
                    'stream': '{stream}/{id}/_definst_/simul-stream.stream live=1 conn=S: conn=S: conn=S: conn=S:{token}'.format(
                        stream=self.STREAM_URL,
                        id=s['id'],
                        token=self.token),
                    'delay': self.DELAY
                })
            # 放送局データを書き込む
            write_json(self.STATION_FILE, buf)
            # 設定データ
            buf = []
            for i, s in enumerate(station):
                buf.append(
                    '    <setting label="{name}" type="bool" id="radiko_{id}" default="true" enable="eq({offset},2)"/>'.format(
                        id=s['id'],
                        name=s['name'],
                        offset=-1-i))
            # 設定データを書き込む
            write_file(self.SETTINGS_FILE, '\n'.join(buf))
        else:
            # 放送局データを書き込む
            write_json(self.STATION_FILE, [])
            # 設定データを書き込む
            write_file(self.SETTINGS_FILE, '')

    def getProgramData(self, renew=False):
        # 初期化
        data = ''
        results = []
        nextupdate = '0'*14
        # キャッシュを確認
        if renew or not os.path.isfile(self.PROGRAM_FILE) or timestamp() > read_file(self.NEXTUPDT_FILE):
            # ウェブから読み込む
            try:
                url = self.PROGRAM_URL % self.area
                if self.area:
                    data = urlread(url, {'Referer':self.REFERER_URL})
                    write_file(self.PROGRAM_FILE, data)
                else:
                    raise urllib2.URLError
            except:
                write_file(self.PROGRAM_FILE, '')
                log('failed to get data from url:%s' % url)
        # キャッシュから番組データを抽出
        data = data or read_file(self.PROGRAM_FILE)
        if data:
            dom = convert(parse(data))
            buf = []
            # 放送局
            station = dom['radiko']['stations']['station']
            station = station if isinstance(station,list) else [station]
            for s in station:
                progs = []
                # 放送中のプログラム
                program = s['scd']['progs']['prog']
                program = program if isinstance(program,list) else [program]
                for p in program:
                    progs.append({
                        'ft': p.get('@ft',''),
                        'ftl': p.get('@ftl',''),
                        'to': p.get('@to',''),
                        'tol': p.get('@tol',''),
                        'title': p.get('title','n/a'),
                        'subtitle': p.get('sub_title',''),
                        'pfm': p.get('pfm',''),
                        'desc': p.get('desc',''),
                        'info': p.get('info',''),
                        'url': p.get('url',''),
                        'content': p.get('content',''),
                        'act': p.get('act',''),
                        'music': p.get('music',''),
                        'free': p.get('free','')
                    })
                results.append({'id':'radiko_%s' % s['@id'], 'progs':progs})
                buf += progs
            # 次の更新時刻
            nextupdate = self.getNextUpdate(buf)
        # 次の更新時刻をファイルに書き込む
        write_file(self.NEXTUPDT_FILE, nextupdate)
        return results, nextupdate
