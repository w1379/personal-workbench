"""Research fit map, separate from official school-policy search and application state.

Canonical reviewed input: config/advisor_map.json. Raw captures are immutable
archive files. Rebuild with import; search is strictly read-only. No networking.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
from db import connect_database, run_write_transaction

ROOT = Path(__file__).resolve().parents[2]
KB = ROOT / 'system/legacy_kb'
DEFAULT = KB / 'config/advisor_map.json'
TABLES = {'advisors': 'research_advisors', 'routes': 'research_routes',
          'links': 'research_advisor_routes', 'evidence': 'research_evidence'}

def validate(payload, connection):
    errors = []
    people = {r['advisor_id'] for r in payload['advisors']}
    routes = {r['route_id'] for r in payload['routes']}
    urls = {r['url'] for r in payload['evidence']}
    if len(people) != len(payload['advisors']) or len(routes) != len(payload['routes']):
        errors.append('重复ID')
    seen_links = set()
    for link in payload['links']:
        key = (link['advisor_id'], link['route_id'])
        if key in seen_links or key[0] not in people or key[1] not in routes:
            errors.append(f'无效关系: {key}')
        seen_links.add(key)
    for row in payload['advisors'] + payload['routes']:
        for url in row['source_urls']:
            if url not in urls:
                errors.append(f'缺来源: {url}')
    for route in payload['routes']:
        target = route['admission_target_id']
        if target:
            parent = connection.execute('SELECT institution FROM admission_targets WHERE target_id=?', (target,)).fetchone()
            if not parent or parent['institution'] != route['institution']:
                errors.append(f'报名目标不存在或学校错配: {target}')
    for source in payload['evidence']:
        path = (ROOT / source['raw_path']).resolve()
        if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
            errors.append(f'缺原件: {path}')
        elif hashlib.sha256(path.read_bytes()).hexdigest() != source['sha256']:
            errors.append(f'哈希不一致: {path}')
    return errors

def encode(row):
    return {key + '_json' if isinstance(value, list) else key:
            json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value
            for key, value in row.items()}

def search(connection, query, line=None):
    aliases = {'西电': '西安电子科技大学', '暨大': '暨南大学', '人大': '中国人民大学', '南大': '南京大学', '中科大': '中国科学技术大学'}
    terms = [aliases.get(term, term) for term in query.casefold().split()]
    result = []
    relevance = {}
    for row in connection.execute('SELECT * FROM research_advisors ORDER BY action_tier,institution,name'):
        item = dict(row)
        if line and line not in json.loads(item['interest_lines_json']):
            continue
        item['routes'] = [dict(r) for r in connection.execute('''
            SELECT r.*, l.mapping_status,l.mapping_basis FROM research_routes r
            JOIN research_advisor_routes l USING(route_id) WHERE l.advisor_id=?
        ''', (item['advisor_id'],))]
        haystack = ' '.join(str(v) for v in item.values()).casefold()
        if not all(term in haystack for term in terms):
            continue
        # Rank a focused evidence field above incidental mentions in separate
        # caveats. Shared routes must not multiply the score for one advisor.
        fields = {str(item.get(key, '')).casefold() for key in
                  ('name', 'institution', 'college', 'research_fact',
                   'training_assessment', 'supervision_fact', 'gaps_json')}
        fields.update(str(route.get(key, '')).casefold()
                      for route in item['routes'] for key in
                      ('program_name', 'channel', 'program_evidence_scope',
                       'selection_rule', 'eligibility_assessment', 'gaps_json'))
        relevance[item['advisor_id']] = (
            sum(term == item['name'].casefold() for term in terms),
            sum(all(term in field for term in terms) for field in fields) if terms else 0,
            sum(min(field.count(term), 3) for field in fields for term in terms),
        )
        result.append(item)
    result.sort(key=lambda item: relevance[item['advisor_id']], reverse=True)
    return result

def render_cards(payload):
    """Render reviewed facts and unresolved mappings without promoting either."""
    destination = KB / f"derived/events/{payload['as_of']}-advisor-map-cards.md"
    people = sorted(payload['advisors'], key=lambda a: (a['action_tier'], a['institution'], a['name']))
    routes = {r['route_id']: r for r in payload['routes']}
    lines = ['# 导师与招生路径卡片', '',
             f"核验批次：{payload['as_of']}。共{len(people)}位导师线索；不是等量可投名额。", '',
             '当前筛选范围：' + payload['scope_note'], '',
             '本页由已审阅的 advisor_map.json 生成。事实、针对用户的判断、尚未核实的事项分别列出；方向标签用于发现，不承诺硕士实际题目。', '',
             'A：微磁学与磁化动力学；B：计算物理与科学机器学习；C：磁性薄膜/自旋材料与器件；D：磁性器件实现神经计算。B不等于每位都做神经网络；团队标签不自动归属个人。', '',
             '| 导师 | 学校 | 方向 | 当前动作 |', '|---|---|---|---|']
    for a in people:
        lines.append(f"| [{a['name']}](#{a['advisor_id']}) | {a['institution']} | {','.join(a['interest_lines'])} | {a['action_tier'].split('_',1)[-1]} |")
    for a in people:
        lines.extend(['', f"<a id=\"{a['advisor_id']}\"></a>", '',
                      f"## {a['institution']} · {a['name']}", '',
                      f"单位：{a['college']}。动作：{a['action_tier'].split('_',1)[-1]}。", '',
                      '**研究依据：** ' + a['research_fact'], '',
                      '**对你的意义（判断）：** ' + a['training_assessment'], '',
                      '**带硕士证据及边界：** ' + a['supervision_fact'], '',
                      '**待核实：** ' + '；'.join(a['gaps']) + '。', ''])
        linked = [l for l in payload['links'] if l['advisor_id'] == a['advisor_id']]
        if not linked:
            lines.extend(['**招生对应：** 尚未建立可核验的当年报名路径；保留研究线索。', ''])
        for link in linked:
            r = routes[link['route_id']]
            lines.extend([f"**{r['entry_year']}入学路径：{r['program_code'] or '代码待核'} {r['program_name']} / {r['degree_type']} / {r['channel']}**", '',
                          '- 对应状态：' + link['mapping_status'] + '。依据：' + link['mapping_basis'],
                          '- 专业证据范围：' + r['program_evidence_scope'],
                          '- 批次时间：' + r['deadline_raw'],
                          '- 选导师规则：' + r['selection_rule'],
                          '- 个人适用条件：' + r['eligibility_assessment'],
                          '- 路径缺口：' + '；'.join(r['gaps']), ''])
            if r['application_url']:
                lines.extend([f"[报名入口]({r['application_url']})", ''])
        urls = list(dict.fromkeys(a['source_urls'] + [u for l in linked for u in routes[l['route_id']]['source_urls']]))
        lines.extend(['**证据入口：**', ''])
        for i,u in enumerate(urls,1):
            lines.append(f'- [来源{i}]({u})')
    destination.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'卡片已生成: {destination}')


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'data/library.sqlite3')
    parser.add_argument('--input', type=Path, default=DEFAULT)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('import')
    sub.add_parser('check')
    sub.add_parser('benchmark')
    sub.add_parser('render')
    find = sub.add_parser('search')
    find.add_argument('query', nargs='?', default='')
    find.add_argument('--line', choices=['A','B','C','D'])
    find.add_argument('--json', action='store_true')
    args = parser.parse_args()
    connection = connect_database(args.db, readonly=args.command != 'import')
    if args.command == 'search':
        rows = search(connection, args.query, args.line)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                print(f"{row['institution']} / {row['name']} / {row['action_tier']}")
                print('  研究事实: ' + row['research_fact'])
                print('  带生证据: ' + row['supervision_fact'])
                for route in row['routes']:
                    print(f"  路径: {route['program_code'] or '代码待核'} {route['program_name']} / {route['channel']} / {route['mapping_status']} / {route['deadline_raw']}")
                print('  待核: ' + '；'.join(json.loads(row['gaps_json'])))
                print('  来源: ' + ' '.join(json.loads(row['source_urls_json'])))
            print(f'共 {len(rows)} 位；多导师可能共用一个报名机会。')
        return
    payload = json.loads(args.input.read_text(encoding='utf-8'))
    errors = validate(payload, connection)
    if errors:
        raise SystemExit('\n'.join(errors))
    if args.command == 'render':
        render_cards(payload)
        connection.close()
        return
    if args.command == 'import':
        connection.executescript((KB / 'schema/advisor_map.sql').read_text(encoding='utf-8'))
        def write(active):
            for kind, table in TABLES.items():
                for raw in payload[kind]:
                    row = encode(raw)
                    columns = list(row)
                    updates = ','.join(f'{col}=excluded.{col}' for col in columns)
                    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT DO UPDATE SET {updates}"
                    active.execute(sql, [row[col] for col in columns])
        run_write_transaction(connection, write)
    if args.command == 'benchmark':
        for case in payload['benchmarks']:
            results = search(connection, case['query'])[:10]
            found = {r['advisor_id'] for r in results}
            found_routes = {route['route_id'] for r in results for route in r['routes']}
            if not (case.get('expected_advisor_ids') or case.get('expected_route_ids')):
                raise SystemExit(f"空检索基准: {case['query']}")
            if (not set(case.get('expected_advisor_ids', [])).issubset(found)
                    or not set(case.get('expected_route_ids', [])).issubset(found_routes)):
                raise SystemExit(f"检索基准失败: {case['query']}")
        print(f"检索基准 {len(payload['benchmarks'])}/{len(payload['benchmarks'])} 通过")
    else:
        for kind, table in TABLES.items():
            count = connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            print(f'{kind}: {count}')
        print('来源、哈希、报名父记录及关系校验通过')
    connection.close()

if __name__ == '__main__':
    main()
