import re
import base64
import json
import ipaddress

def transform(logdata):
    # https://cloudone.trendmicro.com/docs/workload-security/event-syslog-message-formats/
    fields = logdata['message'].split('|')
    if len(fields) < 8:
        print("Illegal format")
        return Null
    logdata.setdefault('agent', {})
    logdata['agent']['name'] = " ".join([fields[1],fields[2],fields[3]])
    logdata.setdefault('rule', {})
    logdata['rule']['name'] = " ".join([fields[4],fields[5]])
    logdata.setdefault('event', {})
    logdata['event']['severity'] = fields[6]
    
    # \\=を適当な文字列に置換しておく
    message = re.sub('\\\\=', '____', fields[7])
    # =をdelimiterとして、順に処理していく
    attributes = message.split('=')
    next_ptr = attributes.pop(0)
    for ptr in attributes:
        values = ptr.split()
        if values is None:
            break
        curr_ptr = next_ptr
        next_ptr = values.pop()
        value = ' '.join(values)
        if value:
            logdata[curr_ptr] = re.sub('____', '=', value)
    # 末尾の処理
    logdata[curr_ptr] = re.sub('____', '=', value + next_ptr)

    if 'act' in logdata:
        # IDS:Resetは、alert出力のみでpacket dropを行わない
        # 誤解を招くので、置換しておく
        logdata['act'] = re.sub("IDS:Reset","DetectOnly:NotReset",logdata['act'])

    # 以下はecsにmappingしていく処理
    deepsecurity_ecs_keys = {
        'destination.ip': 'dst',
        'destination.port': 'dpt',
        'destination.mac': 'dmac',
        'destination.bytes': 'out',
        'source.ip': 'src',
        'source.port': 'spt',
        'source.mac': 'smac',
        'source.bytes': 'in',
        'network.transport': 'proto',
        'event.action': 'act',
        'server.name': 'fluent_hostname',
        'file.path': 'fname',
        'event.count': 'cnt',
        'rule.category': 'cs1',
        'host.id': 'cn1',
        'event.original': 'msg',
    }

    for ecs_key in deepsecurity_ecs_keys:
        original_keys = deepsecurity_ecs_keys[ecs_key]
        v = get_value_from_dict(logdata, original_keys)
        if v:
            new_ecs_dict = put_value_into_dict(ecs_key, v)
            if ".ip" in ecs_key:
                try:
                    ipaddress.ip_address(v)
                except ValueError:
                    continue
            merge(logdata, new_ecs_dict)
            del logdata[original_keys]

    # source.ipが設定されていなければ、dvcで代用する
    if "dvc" in logdata:
        if "source" in logdata and not "ip" in logdata['source']:
            logdata['source']['ip'] = logdata['dvc']
        else:
            logdata['source'] = { 'ip': logdata['dvc'] }

    # packet captureをdecodeしておく
    if 'TrendMicroDsPacketData' in logdata:
        saved = logdata['TrendMicroDsPacketData']
        try:
            logdata['TrendMicroDsPacketData'] = base64.b64decode(logdata['TrendMicroDsPacketData']).decode("utf-8", errors="backslashreplace")
        except Exception as e:
            print(e)
            logdata['TrendMicroDsPacketData'] = saved
        # filter out 'cookie'
        filtered = []
        for line in logdata['TrendMicroDsPacketData'].split("\n"):
            if re.findall(r'^cookie',line.lower()):
                continue
            filtered.append(line)
        logdata['TrendMicroDsPacketData'] = "\n".join(filtered)
        # X-Forwarded-Forを取り出す X-Forwarded-For: 123.123.123.234
        m = re.search(r'X-Forwarded-For: ([0-9.]+)', logdata['TrendMicroDsPacketData'])
        if m:
            logdata['source']['ip'] = m.group(1)

    del logdata['TrendMicroDsTenant'], logdata['TrendMicroDsTenantId']

    return logdata


def put_value_into_dict(key_str, v):
    """dictのkeyにドットが含まれている場合に入れ子になったdictを作成し、値としてvを入れる.
    返値はdictタイプ。vが辞書ならさらに入れ子として代入。
    TODO: 値に"が入ってると例外になる。対処方法が見つからず返値なDROPPEDにしてるので改善する。#34

    >>> put_value_into_dict('a.b.c', 123)
    {'a': {'b': {'c': '123'}}}
    >>> v = {'x': 1, 'y': 2}
    >>> put_value_into_dict('a.b.c', v)
    {'a': {'b': {'c': {'x': 1, 'y': 2}}}}
    >>> v = str({'x': "1", 'y': '2"3'})
    >>> put_value_into_dict('a.b.c', v)
    {'a': {'b': {'c': 'DROPPED'}}}
    """
    v = v
    xkeys = key_str.split('.')
    if isinstance(v, dict):
        json_data = r'{{"{0}": {1} }}'.format(xkeys[-1], json.dumps(v))
    else:
        json_data = r'{{"{0}": "{1}" }}'.format(xkeys[-1], v)
    if len(xkeys) >= 2:
        xkeys.pop()
        for xkey in reversed(xkeys):
            json_data = r'{{"{0}": {1} }}'.format(xkey, json_data)
    try:
        new_dict = json.loads(json_data, strict=False)
    except json.decoder.JSONDecodeError:
        new_dict = put_value_into_dict(key_str, "DROPPED")
    return new_dict

def get_value_from_dict(dct, xkeys_list):
    """ 入れ子になった辞書に対して、dotを含んだkeyで値を
    抽出する。keyはリスト形式で複数含んでいたら分割する。
    値がなければ返値なし

    >>> dct = {'a': {'b': {'c': 123}}}
    >>> xkey = "a.b.c"
    >>> get_value_from_dict(dct, xkey)
    123
    >>> xkey = "x.y.z"
    >>> get_value_from_dict(dct, xkey)

    >>> xkeys_list = "a.b.c x.y.z"
    >>> get_value_from_dict(dct, xkeys_list)
    123
    >>> dct = {'a': {'b': [{'c': 123}, {'c': 456}]}}
    >>> xkeys_list = "a.b.0.c"
    >>> get_value_from_dict(dct, xkeys_list)
    123
    """
    for xkeys in xkeys_list.split():
        v = dct
        for k in xkeys.split('.'):
            try:
                k = int(k)
            except ValueError:
                pass
            try:
                v = v[k]
            except (TypeError, KeyError, IndexError):
                v = ''
                break
        if v:
            return v

def merge(a, b, path=None):
    """merges b into a
    """
    if path is None:
        path = []
    for key in b:
        if key in a:
            if isinstance(a[key], dict) and isinstance(b[key], dict):
                merge(a[key], b[key], path + [str(key)])
            elif a[key] == b[key]:
                pass  # same leaf value
            elif str(a[key]) in str(b[key]):
                # strで上書き。JSONだったのをstrに変換したデータ
                a[key] = b[key]
            else:
                # conflict and override original value with new one
                a[key] = b[key]
        else:
            a[key] = b[key]
    return a

