"""Public statement projection. Never read the private report to build a question."""

def problem_sections(result):
    """(要做什么, 约束与注意事项) 两段文本；旧版题目只有一段。"""
    explanation = result.get('problem_explanation')
    if not explanation:
        if result.get('format') == 'legacy_report' or '_images' in result:
            problem = result.get('problem') or {}
            original = (result.get('_statement') or {}).get('url') or problem.get('url')
            return ('这是旧版题目，未生成新版中文说明。请沿用原题作答，或 /giveup 查看思路；不会自动付费重抽。'
                    + '\n题号：' + str(problem.get('id', ''))
                    + ('\n原题：' + original if isinstance(original, str) and original else ''), '')
        raise ValueError('missing problem explanation')
    if not isinstance(explanation, dict):
        raise ValueError('invalid problem explanation')
    problem_id = (result.get('problem') or {}).get('id')
    if not problem_id or explanation.get('problem_id') != problem_id or explanation.get('language') != 'zh-CN':
        raise ValueError('mismatched problem explanation')
    for name in ('title', 'description', 'input_format', 'output_format', 'original_url'):
        if not isinstance(explanation.get(name), str) or not explanation[name].strip():
            raise ValueError('incomplete problem explanation')
    for name in ('constraints', 'notes'):
        values = explanation.get(name)
        if not isinstance(values, list) or any(not isinstance(s, str) or not s.strip() for s in values):
            raise ValueError('invalid explanation list')
    if not explanation['constraints']:
        raise ValueError('missing constraints')
    samples = explanation.get('samples')
    if not isinstance(samples, list) or any(not isinstance(s, dict) or
            any(not isinstance(s.get(k), str) for k in ('input', 'output')) for s in samples):
        raise ValueError('invalid samples')
    lines = [explanation['title'], '', '题目要你做什么：', explanation['description'],
             '', '输入：', explanation['input_format'], '', '输出：', explanation['output_format']]
    tail = ['约束：', *('- ' + s for s in explanation['constraints'])]
    if explanation['notes']:
        tail.extend(['', '注意事项：', *('- ' + s for s in explanation['notes'])])
    tail.extend(['', '原题：' + explanation['original_url'], 'AI 题意说明可能有误，请结合原题核对。'])
    return '\n'.join(lines), '\n'.join(tail)


def problem_text(result):
    head, tail = problem_sections(result)
    return head + ('\n\n' + tail if tail else '')


def sample_text(result, index=0):
    """一组官方样例，按需取；默认题面里不再夹带样例。"""
    explanation = result.get('problem_explanation')
    if not isinstance(explanation, dict):
        raise ValueError('missing problem explanation')
    samples = explanation.get('samples')
    if not isinstance(samples, list) or not samples:
        return None
    if not 0 <= index < len(samples):
        index = 0
    sample = samples[index]
    body = (sample.get('input') or '').rstrip('\n')
    out = (sample.get('output') or '').rstrip('\n')
    return f'样例 {index + 1} 输入：\n{body}\n样例 {index + 1} 输出：\n{out}'


def problem_pages(result, size=1200):
    # 先按语义切成两段，再对过长的那段按行折页，保证一次只推一段。
    parts = [p for p in problem_sections(result) if p.strip()]
    pages = []
    for part in parts:
        while len(part) > size:
            cut = part.rfind('\n\n', size // 2, size)
            if cut < 0:
                cut = part.rfind('\n', size // 2, size)
            cut = cut + 1 if cut >= 0 else size
            pages.append(part[:cut])
            part = part[cut:]
        pages.append(part)
    return pages
