import sqlite3
import urllib

from support import SupportFile

from .plex_db import PlexDBHandle, dict_factory
from .setup import *


class Task(object):

    source_con = source_cur = None
    target_con = target_cur = None
    change_rule = None
    change_rule_extra = None
    SOURCE_SECTION_ID = None
    SOURCE_LOCATIONS = None
    TARGET_SECTION_ID = None
    TARGET_LOCATIONS = None
    config = None

    @staticmethod
    @F.celery.task(bind=True)
    def start(self, *args):
        try:
            Task.config = P.load_config()
            Task.change_rule = [P.ModelSetting.get('copy_copy_path_source_root_path'), P.ModelSetting.get('copy_copy_path_target_root_path')]
            Task.file_change_rule = [P.ModelSetting.get('copy_copy_path_source_root_path'), P.ModelSetting.get('copy_copy_path_target_root_path').replace(' ', '%20')]
            Task.TARGET_SECTION_ID = P.ModelSetting.get('copy_copy_target_section_id')

            Task.source_con = sqlite3.connect(P.ModelSetting.get('copy_copy_path_source_db'))
            Task.source_cur = Task.source_con.cursor()
            Task.target_con = sqlite3.connect(P.ModelSetting.get('base_path_db'))
            Task.target_cur = Task.target_con.cursor()

            ce = Task.source_con.execute('SELECT * FROM library_sections')
            ce.row_factory = dict_factory
            data = ce.fetchall()
            if len(data) == 1:
                Task.SOURCE_SECTION_ID = data[0]['id']
                ce = Task.source_con.execute('SELECT * FROM section_locations WHERE library_section_id = ?', (Task.SOURCE_SECTION_ID,))
                ce.row_factory = dict_factory
                Task.SOURCE_LOCATIONS = ce.fetchall()

                ce = Task.target_con.execute('SELECT * FROM section_locations WHERE library_section_id = ?', (Task.TARGET_SECTION_ID,))
                ce.row_factory = dict_factory
                Task.TARGET_LOCATIONS = ce.fetchall()

                if data[0]['section_type'] == 1:
                    return Task.movie_start(self)
                elif data[0]['section_type'] == 2:
                    return Task.tv_start(self)
                elif data[0]['section_type'] == 8:
                    return Task.music_start(self)
     
        except Exception as e: 
            P.logger.error(f'Exception:{str(e)}')
            P.logger.error(traceback.format_exc())
        finally:
            if Task.source_cur is not None:
                Task.source_cur.close()
            if Task.source_con is not None:
                Task.source_con.close()
            if Task.target_cur is not None:
                Task.target_cur.close()
            if Task.target_con is not None:
                Task.target_con.close()
    


    @staticmethod
    def movie_start(celery_instance):
        status = {'is_working':'run', 'count':0, 'current':0}
        for SOURCE_LOCATION in Task.SOURCE_LOCATIONS:
            CURRENT_TARGET_LOCATION_ID, CURRENT_TARGET_LOCATION_FOLDERPATH = Task.get_target_location_id(SOURCE_LOCATION)
            try:
                ce = Task.source_con.execute(Task.config['라이브러리 복사 영화 쿼리'], (SOURCE_LOCATION['id'],))
            except Exception as e: 
                P.logger.error(f'Exception:{str(e)}')
                P.logger.error(traceback.format_exc())
                ce = Task.source_con.execute('SELECT * FROM metadata_items WHERE metadata_type = 1 AND id in (SELECT metadata_item_id FROM media_items WHERE section_location_id = ?) ORDER BY title DESC', (SOURCE_LOCATION['id'],))
    
            ce.row_factory = dict_factory
            meta_list = ce.fetchall()
            status['count'] += len(meta_list)

            for idx, metadata_item in enumerate(meta_list):
                if P.ModelSetting.get_bool('copy_status_task_stop_flag'):
                    return 'stop'
                try:
                    status['current'] += 1
                    data = {'status':status, 'ret':'append', 'title':metadata_item['title'], 'year':metadata_item['year'], 'files':[]}
                    metadata_item_id, is_exist = Task.insert_metadata_items(metadata_item, Task.TARGET_SECTION_ID)
                    if is_exist:
                        data['ret'] = 'exist'
                        continue
                    new_filename = None
                    media_ce = Task.source_con.execute('SELECT * FROM media_items WHERE metadata_item_id = ? ORDER BY id', (metadata_item['id'],))
                    media_ce.row_factory = dict_factory
                    for media_item in media_ce.fetchall():
                        media_item_id = Task.insert_media_items(media_item, Task.TARGET_SECTION_ID, CURRENT_TARGET_LOCATION_ID, metadata_item_id)
                        part_ce = Task.source_con.execute('SELECT * FROM media_parts WHERE media_item_id = ? ORDER BY id', (media_item['id'],))
                        part_ce.row_factory = dict_factory
                        for media_part in part_ce.fetchall():
                            media_part_id, new_filename = Task.insert_media_parts(media_part, media_item_id, Task.TARGET_SECTION_ID, CURRENT_TARGET_LOCATION_FOLDERPATH)
                            data['files'].append(new_filename)
                            stream_ce = Task.source_con.execute('SELECT * FROM media_streams WHERE media_item_id = ? AND media_part_id = ? ORDER BY id', (media_item['id'],media_part['id']))
                            stream_ce.row_factory = dict_factory
                            for media_stream in stream_ce.fetchall():
                                media_stream_id = Task.insert_media_streams(media_stream, media_item_id, media_part_id, Task.TARGET_SECTION_ID)
                    Task.insert_tag(metadata_item, metadata_item_id)

                    # 2021-10-17
                    # 부가항목
                    # 메타는 쓰여지나 미디어 처리는 하다가 중단.
                    # db make 단계부터 라이브러리 id를 기준으로 처리를 많이하나 부가영상은은 라이브러리 소속이 아니다.
                    # 개별로 metadata_item 이며 relations 테이블을 통해 연결된다. make단계부터 이를 고려하여 db를 생성해야하나 노력에 비해 효과가 미비할 것으로 보인다.
                    # media_item, pars, stream은 그냥 코드를 가져다 써도 되겠지만 directory를 테스트하기도 빡세다.
                    #Task.process_extra(metadata_item, metadata_item_id)
                except Exception as e: 
                    P.logger.error(f'Exception:{str(e)}')
                    P.logger.error(traceback.format_exc())
                finally:
                    if F.config['use_celery']:
                        celery_instance.update_state(state='PROGRESS', meta=data)
                    else:
                        celery_instance.receive_from_task(data, celery=False)
        return 'wait'                

    @staticmethod
    def process_extra(metadata_item, new_metadata_item_id):
        try:
            relation_ce = Task.source_con.execute('SELECT * FROM metadata_relations WHERE metadata_item_id = ? ORDER BY related_metadata_item_id', (metadata_item['id'],))
            relation_ce.row_factory = dict_factory
            for relation in relation_ce.fetchall():
                try:
                    relation_metadata_ce = Task.source_con.execute('SELECT * FROM metadata_items WHERE id = ?', (relation['related_metadata_item_id'],))
                    relation_metadata_ce.row_factory = dict_factory
                    relation_metadata_item = relation_metadata_ce.fetchall()[0]
                    insert_col = insert_value = ''
                    for key, value in relation_metadata_item.items():
                        if key in ['id'] or value is None:
                            continue
                        if key == 'user_thumb_url' and value is not None and value.startswith('media') and metadata_item['user_art_url'] is not None and metadata_item['user_art_url'].startswith('http'):
                            value = metadata_item['user_art_url']
                        if key == 'guid' and value.startswith('file://'):
                            value = Task.change_extra_guid(value)
                        insert_col += f"'{key}',"
                        if type(value) == type(''):
                            value = value.replace('"', '""')
                            insert_value += f'"{value}",'
                        else:
                            insert_value += f"{value},"
                    insert_col = insert_col.rstrip(',')
                    insert_value = insert_value.rstrip(',')
                    query = f"INSERT INTO metadata_items({insert_col}) VALUES({insert_value});SELECT max(id) FROM metadata_items;" 
                    insert_col = insert_value = ''
                    ret = PlexDBHandle.execute_query(query)
                    if ret != '':
                        new_extra_metadata_item_id = int(ret)
                    else:
                        P.logger.error('insert fail!!')
                        continue
                    for key, value in relation.items():
                        if key in ['id'] or value is None:
                            continue
                        if key == 'metadata_item_id':
                            value = new_metadata_item_id
                        if key == 'related_metadata_item_id':
                            value = new_extra_metadata_item_id
                        insert_col += f"'{key}',"
                        if type(value) == type(''):
                            value = value.replace('"', '""')
                            insert_value += f'"{value}",'
                        else:
                            insert_value += f"{value},"
                    insert_col = insert_col.rstrip(',')
                    insert_value = insert_value.rstrip(',')
                    query = f"INSERT INTO metadata_relations({insert_col}) VALUES({insert_value});" 
                    ret = PlexDBHandle.execute_query(query)
                    P.logger.warning(ret)
                except Exception as e: 
                    P.logger.error(f'Exception:{str(e)}')
                    P.logger.error(traceback.format_exc())
            return True
        except Exception as e: 
            P.logger.error(f'Exception:{str(e)}')
            P.logger.error(traceback.format_exc())



    @staticmethod
    def tv_start(celery_instance):
        status = {'is_working':'run', 'count':0, 'current':0}
        for SOURCE_LOCATION in Task.SOURCE_LOCATIONS:
            CURRENT_TARGET_LOCATION_ID, CURRENT_TARGET_LOCATION_FOLDERPATH = Task.get_target_location_id(SOURCE_LOCATION)
            if CURRENT_TARGET_LOCATION_ID is None:
                P.logger.error(f"CURRENT_TARGET_LOCATION_ID is None. {SOURCE_LOCATION}")
                continue
                return 'fail'
            try:
                ce = Task.source_con.execute(Task.config['라이브러리 복사 TV 쿼리'], (SOURCE_LOCATION['id'],))
            except Exception as e: 
                P.logger.error(f'Exception:{str(e)}')
                P.logger.error(traceback.format_exc())
                ce = Task.source_con.execute('SELECT * FROM metadata_items WHERE id in (SELECT parent_id FROM metadata_items WHERE id in (SELECT parent_id FROM metadata_items WHERE id in (SELECT metadata_item_id FROM media_items WHERE section_location_id = ?) GROUP BY parent_id) GROUP BY parent_id) ORDER BY title DESC', (SOURCE_LOCATION['id'],))
            ce.row_factory = dict_factory
            meta_list = ce.fetchall()
            status['count'] += len(meta_list)
            for idx, show_metadata_item in enumerate(meta_list):
                if P.ModelSetting.get_bool('copy_status_task_stop_flag'):
                    return 'stop'
                try:
                    status['current'] += 1
                    data = {'status':status, 'ret':'append', 'title':show_metadata_item['title'], 'year':show_metadata_item['year'], 'files':[]}
                    P.logger.warning(f"{idx} / {len(meta_list)} {show_metadata_item['title']}")
                    show_metadata_item_id, is_exist = Task.insert_metadata_items(show_metadata_item, Task.TARGET_SECTION_ID)
                    if is_exist:
                        data['ret'] = 'exist'
                        continue
                    season_ce = Task.source_con.execute('SELECT * FROM metadata_items WHERE parent_id = ? ORDER BY `index`', (show_metadata_item['id'],))
                    season_ce.row_factory = dict_factory
                    for season_metadata_item in season_ce.fetchall():
                        season_metadata_item_id, is_exist = Task.insert_metadata_items(season_metadata_item, Task.TARGET_SECTION_ID, parent_id=show_metadata_item_id)
                        episode_ce = Task.source_con.execute('SELECT * FROM metadata_items WHERE parent_id = ? ORDER BY `index`', (season_metadata_item['id'],))
                        episode_ce.row_factory = dict_factory
                        for episode_metadata_item in episode_ce.fetchall():
                            episode_metadata_item_id, is_exist = Task.insert_metadata_items(episode_metadata_item, Task.TARGET_SECTION_ID, parent_id=season_metadata_item_id)
                            new_filename = None
                            media_ce = Task.source_con.execute('SELECT * FROM media_items WHERE metadata_item_id = ? ORDER BY id', (episode_metadata_item['id'],))
                            media_ce.row_factory = dict_factory
                            for media_item in media_ce.fetchall():
                                media_item_id = Task.insert_media_items(media_item, Task.TARGET_SECTION_ID, CURRENT_TARGET_LOCATION_ID, episode_metadata_item_id)
                                part_ce = Task.source_con.execute('SELECT * FROM media_parts WHERE media_item_id = ? ORDER BY id', (media_item['id'],))
                                part_ce.row_factory = dict_factory
                                for media_part in part_ce.fetchall():
                                    media_part_id, new_filename = Task.insert_media_parts(media_part, media_item_id, Task.TARGET_SECTION_ID, CURRENT_TARGET_LOCATION_FOLDERPATH)
                                    data['files'].append(new_filename)
                                    stream_ce = Task.source_con.execute('SELECT * FROM media_streams WHERE media_item_id = ? AND media_part_id = ? ORDER BY id', (media_item['id'],media_part['id']))
                                    stream_ce.row_factory = dict_factory
                                    for media_stream in stream_ce.fetchall():
                                        media_stream_id = Task.insert_media_streams(media_stream, media_item_id, media_part_id, Task.TARGET_SECTION_ID)
                    Task.insert_tag(show_metadata_item, show_metadata_item_id)
                except Exception as e: 
                    P.logger.error(f'Exception:{str(e)}')
                    P.logger.error(traceback.format_exc())
                finally:
                    if F.config['use_celery']:
                        celery_instance.update_state(state='PROGRESS', meta=data)
                    else:
                        celery_instance.receive_from_task(data, celery=False)
        return 'wait'
          

    @staticmethod
    def music_start(celery_instance):
        status = {'is_working':'run', 'count':0, 'current':0}
        for SOURCE_LOCATION in Task.SOURCE_LOCATIONS:
            CURRENT_TARGET_LOCATION_ID, CURRENT_TARGET_LOCATION_FOLDERPATH = Task.get_target_location_id(SOURCE_LOCATION)
            if CURRENT_TARGET_LOCATION_ID is None:
                return 'fail'
            try:
                ce = Task.source_con.execute(Task.config['라이브러리 복사 음악 쿼리'], (SOURCE_LOCATION['id'],))
            except Exception as e: 
                P.logger.error(f'Exception:{str(e)}')
                P.logger.error(traceback.format_exc())
                ce = Task.source_con.execute('SELECT * FROM metadata_items WHERE id in (SELECT parent_id FROM metadata_items WHERE id in (SELECT parent_id FROM metadata_items WHERE id in (SELECT metadata_item_id FROM media_items WHERE section_location_id = ?) GROUP BY parent_id) GROUP BY parent_id) ORDER BY title DESC', (SOURCE_LOCATION['id'],))
            ce.row_factory = dict_factory
            meta_list = ce.fetchall()
            status['count'] += len(meta_list)
            for idx, artist_metadata_item in enumerate(meta_list):
                if P.ModelSetting.get_bool('copy_status_task_stop_flag'):
                    return 'stop'
                try:
                    status['current'] += 1
                    data = {'status':status, 'ret':'append', 'title':artist_metadata_item['title'], 'year':'', 'files':[]}
                    P.logger.warning(f"{idx} / {len(meta_list)} {artist_metadata_item['title']}")
                    artist_metadata_item_id, is_exist = Task.insert_metadata_items(artist_metadata_item, Task.TARGET_SECTION_ID)
                    if is_exist:
                        data['ret'] = 'exist'
                        continue
                    album_ce = Task.source_con.execute('SELECT * FROM metadata_items WHERE parent_id = ? ORDER BY `index`', (artist_metadata_item['id'],))
                    album_ce.row_factory = dict_factory
                    for album_metadata_item in album_ce.fetchall():
                        album_metadata_item_id, is_exist = Task.insert_metadata_items(album_metadata_item, Task.TARGET_SECTION_ID, parent_id=artist_metadata_item_id)
                        track_ce = Task.source_con.execute('SELECT * FROM metadata_items WHERE parent_id = ? ORDER BY `index`', (album_metadata_item['id'],))
                        track_ce.row_factory = dict_factory
                        for track_metadata_item in track_ce.fetchall():
                            track_metadata_item_id, is_exist = Task.insert_metadata_items(track_metadata_item, Task.TARGET_SECTION_ID, parent_id=album_metadata_item_id)                 
                            new_filename = None
                            media_ce = Task.source_con.execute('SELECT * FROM media_items WHERE metadata_item_id = ? ORDER BY id', (track_metadata_item['id'],))
                            media_ce.row_factory = dict_factory
                            for media_item in media_ce.fetchall():
                                media_item_id = Task.insert_media_items(media_item, Task.TARGET_SECTION_ID, CURRENT_TARGET_LOCATION_ID, track_metadata_item_id)
                                part_ce = Task.source_con.execute('SELECT * FROM media_parts WHERE media_item_id = ? ORDER BY id', (media_item['id'],))
                                part_ce.row_factory = dict_factory
                                for media_part in part_ce.fetchall():
                                    media_part_id, new_filename = Task.insert_media_parts(media_part, media_item_id, Task.TARGET_SECTION_ID, CURRENT_TARGET_LOCATION_FOLDERPATH)
                                    data['files'].append(new_filename)
                                    stream_ce = Task.source_con.execute('SELECT * FROM media_streams WHERE media_item_id = ? AND media_part_id = ? ORDER BY id', (media_item['id'],media_part['id']))
                                    stream_ce.row_factory = dict_factory
                                    for media_stream in stream_ce.fetchall():
                                        media_stream_id = Task.insert_media_streams(media_stream, media_item_id, media_part_id, Task.TARGET_SECTION_ID)
                    Task.insert_tag(artist_metadata_item, artist_metadata_item_id)
                except Exception as e: 
                    P.logger.error(f'Exception:{str(e)}')
                    P.logger.error(traceback.format_exc())
                finally:
                    if F.config['use_celery']:
                        celery_instance.update_state(state='PROGRESS', meta=data)
                    else:
                        celery_instance.receive_from_task(data, celery=False)
        return 'wait'


    @staticmethod
    def insert_media_streams(media_stream, media_item_id, media_part_id, library_section_id):
        data = PlexDBHandle.select_arg(f"SELECT id FROM media_streams WHERE media_item_id = ? AND media_part_id = ? AND stream_type_id = ? AND codec = ? AND language = ? AND `index` = ? AND extra_data = ?", (media_item_id, media_part_id, media_stream['stream_type_id'], media_stream['codec'], media_stream['language'], media_stream['index'], media_stream['extra_data']))
        #logger.error(data)
        if len(data) == 1:
            return data[0]['id']
        elif len(data) == 0:
            insert_col = ''
            insert_value = ''
            for key, value in media_stream.items():
                if key in ['id']:
                    continue
                if key == 'media_item_id':
                    value = media_item_id
                if key == 'media_part_id':
                    value = media_part_id
                if key == 'url':
                    if value != '' and value.startswith('file'):
                        value = value.replace(Task.file_change_rule[0], Task.file_change_rule[1])
                if value is None:
                    continue
                insert_col += f"'{key}',"
                if type(value) == type(''):
                    value = value.replace('"', '""')
                    insert_value += f'"{value}",'
                else:
                    insert_value += f"{value},"
            insert_col = insert_col.rstrip(',')
            insert_value = insert_value.rstrip(',')
            query = f"INSERT INTO media_streams ({insert_col}) VALUES ({insert_value});SELECT max(id) FROM media_streams;" 
            ret = PlexDBHandle.execute_query(query)
            if ret != '':
                return int(ret)
        else:
            P.logger.error("동일 정보가 여러가 있음")


    @staticmethod
    def change_extra_guid(source):
        #logger.error(source)
        #source = source.replace('file://', '')
        # 한글 quote처리. _plus인지 확인필요
        if Task.change_rule_extra is None:
            Task.change_rule_extra = []
            for rule in Task.change_rule:
                rule_extra = []
                if rule[0] != '/':
                    sp = rule.split('\\')
                else:
                    sp = rule.split('/')
                for tmp in sp:
                    rule_extra.append(urllib.request.quote(json.dumps(tmp).strip('"')).replace('%3A', ':'))
                tmp2 = '/'.join(rule_extra)
                if tmp2[0] != '/':
                    tmp2 = '/' + tmp2
                Task.change_rule_extra.append(tmp2)
                #P.logger.warning(Task.change_rule_extra)
        target = source.replace(Task.change_rule_extra[0], Task.change_rule_extra[1])
        #logger.warning(target)
        return target

    @staticmethod
    def process_localfile(filepath, library_section_id, current_section_folderpath):
        new_filepath = filepath.replace(Task.change_rule[0], Task.change_rule[1])
        if Task.change_rule[1][0] != '/': #windows
            new_filepath = new_filepath.replace('/', '\\')
            text = new_filepath.replace(current_section_folderpath + '\\', '')
            folderpath = '/'.join(text.split('\\')[:-1])
        else:
            new_filepath = new_filepath.replace('\\', '/')
            text = new_filepath.replace(current_section_folderpath + '/', '')
            folderpath = '/'.join(text.split('/')[:-1])
        #logger.warning(f"새로운 경로 : {new_filepath}")
        #라이브러리 폴더 root_path
        
        ########################################
        ######################################
        #  짧게 쓴경우 이게 문제 발생 2021-10-12
        #######################################
        
        #text = filepath.replace(Task.change_rule[0] + '/', '')
        #logger.debug(text)
        #folderpath = '/'.join(text.split('/')[:-1])
        ret = {}
        ret['new_filepath'] = new_filepath
        ret['dir_id'] = Task.make_directories(library_section_id, folderpath)
        #logger.debug(ret)
        return ret
    
    # 2021-10-12 중대오류
    # 라이브러리 폴더 지정 이후의 값만 와야함.
    # 영화 : 영화/가/가나다 (1999) 영화를 
    #   폴더 : 영화 로 한경우 - NULL -> 가/가나다(1999)
    #   폴더 : 영화/가 로 지정한경우 - NULL -> 가나다(1999)

    # 쇼 : 쇼는 무조건 컨텐츠 폴더가 와야한다
    # 애니/가/가나다/시즌1 인경우
    # 라이브러리 폴더는 애니/가  필수
    # NULL -> 가나다
    # NULL -> 가나다/시즌1 생성

    # 여기서는 그 섹션의 루트 폴더를 받아서 처리
    @staticmethod
    def make_directories(library_section_id, path):
        data = PlexDBHandle.select_arg(f"SELECT id FROM directories WHERE library_section_id = ? AND path = ?", (library_section_id, path))
        if len(data) == 1:
            return data[0]['id']
        elif len(data) == 0:
            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            updated_str = time_str
            if path == '':
                # '' parent 를 구하기 위해서 왔는데 DB 없다
                query = f"INSERT INTO directories ('library_section_id','path','created_at','updated_at') VALUES ('{library_section_id}','{path}','{time_str}','{updated_str}');SELECT max(id) FROM directories;" 
                ret = PlexDBHandle.execute_query(query)
                if ret != '':
                    return int(ret)
            else:
                tmps = path.split('/')
                if len(tmps) == 1:
                    parent_path = ''
                else:
                    parent_path = '/'.join(tmps[:-1])
            parent_directory_id = Task.make_directories(library_section_id, parent_path)
            try:
                updated_at_ce = Task.source_con.execute('SELECT updated_at FROM directories WHERE path = ?', (path,))
                updated_at_ce.row_factory = dict_factory
                tmp = updated_at_ce.fetchall()
                if len(tmp) == 1:
                    updated_str = tmp[0]['updated_at']
            except Exception as e: 
                P.logger.error(f'Exception:{str(e)}')
                P.logger.error(traceback.format_exc())                
            path = path.replace("'", "''")
            query = f"INSERT INTO directories ('library_section_id','parent_directory_id','path','created_at','updated_at') VALUES ('{library_section_id}',{parent_directory_id},'{path}','{time_str}','{updated_str}');SELECT max(id) FROM directories;" 
            ret = PlexDBHandle.execute_query(query)
            if ret != '':
                return int(ret)
    


    @staticmethod
    def insert_media_parts(media_part, media_item_id, library_section_id, current_section_folderpath):
        data = PlexDBHandle.select_arg(f"SELECT id FROM media_parts WHERE hash = ? AND media_item_id = ?", (media_part['hash'], media_item_id))
        if len(data) >= 1:
            return data[0]['id'], None
        elif len(data) == 0:
            file_ret = Task.process_localfile(media_part['file'], library_section_id, current_section_folderpath)
            insert_col = ''
            insert_value = ''
            for key, value in media_part.items():
                if key in ['id']:
                    continue
                if key == 'media_item_id':
                    value = media_item_id
                if key == 'directory_id':
                    value = file_ret['dir_id']
                if key == 'file':
                    value = file_ret['new_filepath']
                if value is None:
                    continue
                insert_col += f"'{key}',"
                if type(value) == type(''):
                    value = value.replace('"', '""')
                    insert_value += f'"{value}",'
                else:
                    insert_value += f"{value},"
            insert_col = insert_col.rstrip(',')
            insert_value = insert_value.rstrip(',')
            query = f"INSERT INTO media_parts ({insert_col}) VALUES ({insert_value});SELECT max(id) FROM media_parts;" 
            ret = PlexDBHandle.execute_query(query)
            if ret != '':
                return int(ret), file_ret['new_filepath']
        else:
            P.logger.error("동일 정보가 여러가 있음")



    @staticmethod
    def insert_media_items(media_item, library_section_id, section_location_id, metadata_item_id, insert=True):
        data = PlexDBHandle.select_arg(f"SELECT id FROM media_items WHERE library_section_id = ? AND metadata_item_id = ? AND size = ? AND bitrate = ? AND hints = ?", (library_section_id, metadata_item_id, media_item['size'], media_item['bitrate'], media_item['hints']))
        if len(data) >= 1:
            return data[0]['id']
        elif len(data) == 0:
            if insert:
                insert_col = ''
                insert_value = ''
                for key, value in media_item.items():
                    if key in ['id']:
                        continue
                    if key == 'library_section_id':
                        value = library_section_id
                    if key == 'section_location_id':
                        value = section_location_id
                    if key == 'metadata_item_id':
                        value = metadata_item_id
                    if value is None:
                        continue
                    insert_col += f"'{key}',"
                    if type(value) == type(''):
                        value = value.replace('"', '""')
                        insert_value += f'"{value}",'
                    else:
                        insert_value += f"{value},"
                insert_col = insert_col.rstrip(',')
                insert_value = insert_value.rstrip(',')
                query = f"INSERT INTO media_items ({insert_col}) VALUES ({insert_value});SELECT max(id) FROM media_items;" 
                ret = PlexDBHandle.execute_query(query)
                if ret != '':
                    return int(ret)
            else:
                P.logger.error("insert 했으나 정보 없음")    
        else:
            P.logger.error("동일 정보가 여러가 있음")


    @staticmethod
    def insert_metadata_items(metadata_item, section_id, insert=True, parent_id=None):
        data = PlexDBHandle.select_arg(f"SELECT id FROM metadata_items WHERE library_section_id = ? AND guid = ? AND hash = ?", (section_id, metadata_item['guid'], metadata_item['hash']))        
        if len(data) >= 1:
            return data[0]['id'], True
        elif len(data) == 0:
            if insert:
                insert_col = ''
                insert_value = ''
                for key, value in metadata_item.items():
                    if key in ['id']:
                        continue
                    if key == 'library_section_id':
                        value = section_id
                    if value is None:
                        continue
                    if key == 'parent_id' and parent_id is not None:
                        value = parent_id
                    insert_col += f"'{key}',"
                    if type(value) == type(''):
                        value = value.replace('"', '""')
                        insert_value += f'"{value}",'
                    else:
                        insert_value += f"{value},"
                insert_col = insert_col.rstrip(',')
                insert_value = insert_value.rstrip(',')
                query = f"INSERT INTO metadata_items({insert_col}) VALUES({insert_value});SELECT max(id) FROM metadata_items;" 
                ret = PlexDBHandle.execute_query(query)
                if ret != '':
                    return int(ret), False
            else:
                P.logger.error("insert 했으나 정보 없음")    
        else:
            P.logger.error("동일 정보가 여러가 있음")


    @staticmethod
    def get_target_location_id(SOURCE_LOCATION):
        root_path = SOURCE_LOCATION['root_path']
        new_root_path = root_path.replace(Task.change_rule[0], Task.change_rule[1])
        if Task.change_rule[1][0] != '/': #windows
            new_root_path = new_root_path.replace('/', '\\')
        for TARGET_LOCATION in Task.TARGET_LOCATIONS:
            if TARGET_LOCATION['root_path'] == new_root_path:
                return TARGET_LOCATION['id'], TARGET_LOCATION['root_path']
        return None, None


    @staticmethod
    def create_info_xml(metadata_item, metadata_type):
        row_ce = Task.source_con.execute('SELECT hash, data FROM metadata WHERE hash = ?', (metadata_item['hash'],))
        row_ce.row_factory = dict_factory
        row = row_ce.fetchall()
        if len(row) == 1:
            metapath = os.path.join(P.ModelSetting.get('base_path_metadata'), 'Movies' if metadata_type == 1 else 'TV Shows', metadata_item['hash'][0], f"{metadata_item['hash'][1:]}.bundle", 'Contents', '_combined', 'Info.xml')
            if os.path.exists(metapath):
                P.logger.warning(f"{metadata_item['title']} Info.xml already exist..")
            else:
                folder_path = os.path.dirname(metapath)
                if os.path.exists(folder_path) == False:
                    os.makedirs(folder_path)
                    SupportFile.write_file(metapath, row[0]['data'])
                    P.logger.debug(metapath)
                    P.logger.warning(f"{metadata_item['title']} Info.xml write..")
        else:
            P.logger.warning('info.xml data not exist')                


    @staticmethod
    def insert_tag(metadata_item, plex_metadata_item_id):      
        row_ce = Task.source_con.execute('SELECT taggings.tag_id, taggings.`index` AS taggings_index, taggings.text AS taggings_text, taggings.time_offset AS taggings_time_offset, taggings.end_time_offset AS taggings_end_time_offset, taggings.created_at AS taggings_created_at, taggings.extra_data AS taggings_extra_data, tags.tag AS tags_tag, tags.tag_type AS tags_tag_type, tags.user_thumb_url AS tags_user_thumb_url, tags.created_at AS tags_created_at, tags.updated_at AS tags_updated_at FROM taggings, tags WHERE taggings.tag_id = tags.id AND taggings.metadata_item_id = ? ORDER BY taggings.id', (metadata_item['id'],))
        row_ce.row_factory = dict_factory
        rows = row_ce.fetchall()
        for tag_item in rows:
            if tag_item['taggings_index'] is not None:
                data = PlexDBHandle.select_arg(f"SELECT * FROM taggings, tags WHERE taggings.tag_id = tags.id AND taggings.metadata_item_id = ? AND taggings.`index` = ? AND taggings.text = ? AND taggings.extra_data = ? AND tags.tag = ? AND tags.tag_type = ?", (plex_metadata_item_id, tag_item['taggings_index'], tag_item['taggings_text'], tag_item['taggings_extra_data'], tag_item['tags_tag'], tag_item['tags_tag_type']))
            else:
                data = PlexDBHandle.select_arg(f"SELECT * FROM taggings, tags WHERE taggings.tag_id = tags.id AND taggings.metadata_item_id = ? AND taggings.text = ? AND taggings.extra_data = ? AND tags.tag = ? AND tags.tag_type = ?", (plex_metadata_item_id, tag_item['taggings_text'], tag_item['taggings_extra_data'], tag_item['tags_tag'], tag_item['tags_tag_type']))
            if len(data) > 0:
                continue
            tag_id = -1
            data = PlexDBHandle.select_arg(f"SELECT * FROM tags WHERE tag = ? AND tag_type = ? AND user_thumb_url = ?", (tag_item['tags_tag'], tag_item['tags_tag_type'], tag_item['tags_user_thumb_url']))
            if len(data) > 0:
                tag_id = data[0]['id']
            elif len(data) == 0:
                insert_col = "'tag', 'tag_type', 'user_thumb_url', 'updated_at', 'user_art_url', 'user_music_url', 'extra_data', 'key'"
                value = tag_item["tags_tag"].replace('"', '""')
                insert_value = f'"{value}", {tag_item["tags_tag_type"]}, "{tag_item["tags_user_thumb_url"]}", "{tag_item["tags_updated_at"]}", "", "", "", ""'
                if tag_item["tags_created_at"] is not None:
                    insert_col += ", 'created_at'"
                    insert_value += f', "{tag_item["tags_created_at"]}"'
                query = f"INSERT INTO tags({insert_col}) VALUES ({insert_value});SELECT max(id) FROM tags;" 
                ret = PlexDBHandle.execute_query(query)
                if ret != '':
                    tag_id = int(ret)           
            if tag_id == -1:
                continue
            insert_col = "'metadata_item_id', 'tag_id', 'created_at', 'text',"
            value = tag_item["taggings_text"].replace('"', '""')
            insert_value = f'{plex_metadata_item_id}, {tag_id}, "{tag_item["taggings_created_at"]}", "{value}",'
            if tag_item["taggings_index"] is not None:
                insert_col += " `index`,"
                insert_value += f' {tag_item["taggings_index"]},'
            if tag_item["taggings_time_offset"] is not None:
                insert_col += " 'time_offset', 'end_time_offset',"
                insert_value += f' {tag_item["taggings_time_offset"]}, {tag_item["taggings_end_time_offset"]},'
            if tag_item["taggings_extra_data"] is not None:
                insert_col += " 'extra_data',"
                value = tag_item["taggings_extra_data"].replace('"', '""')
                insert_value += f'"{value}",'
            insert_col += " 'thumb_url'"
            insert_value += f' ""'
            query = f"INSERT INTO taggings({insert_col}) VALUES ({insert_value});SELECT max(id) FROM taggings;" 
            ret = PlexDBHandle.execute_query(query)
            if ret != '':
                taggins_id = int(ret)
            
