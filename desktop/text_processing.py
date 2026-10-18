"""Ordered, hierarchical splitting; whole sentences move before being subdivided."""
import re

DEFAULT_PROCESSING=dict(priorities=['paragraph','sentence','comma','off'],
                        remove_linebreaks=True,space_cycles=3,paragraph_cycles=3,
                        remove_symbols=False,symbols='";«;»;„;“;”;‟;‘;’;‚;‛;‹;›;`;\t;\u200b;\ufeff')
PATTERNS={
    'paragraph':r'\n+',
    'sentence':r'[.!?…]+[»”"\)]*(?:\s+|$)',
    'comma':r',\s*',
    'space':r'[^\S\n]+',
}


def symbol_list(value):
    result=[];current='';escaped=False
    for char in value:
        if escaped:current+=char if char in (';','^') else '^'+char;escaped=False
        elif char=='^':escaped=True
        elif char==';':
            if current:result.append(current)
            current=''
        else:current+=char
    if escaped:current+='^'
    if current:result.append(current)
    return result


def clean_text(text,options,remove_lines=None):
    if options.get('remove_symbols'):
        for symbol in sorted(set(symbol_list(options.get('symbols',''))),key=len,reverse=True):
            text=text.replace(symbol,'')
    text=text.replace('\r\n','\n').replace('\r','\n')
    for _ in range(max(0,min(100,int(options.get('paragraph_cycles',0))))):
        text=text.replace('\n\n','\n')
    if options.get('remove_linebreaks',False) if remove_lines is None else remove_lines:
        text=re.sub(r' *\n+ *',' ',text)
    for _ in range(max(0,min(100,int(options.get('space_cycles',0))))):
        text=text.replace('  ',' ')
    return text


def iter_fragments(text,limit=300,options=None):
    if not 20<=limit<=2400:raise ValueError('Размер фрагмента: от 20 до 2400 символов')
    options=options or {}
    text=clean_text(text,options,remove_lines=False)
    if not text.strip():return
    priorities=[]
    for key in options.get('priorities',DEFAULT_PROCESSING['priorities']):
        if key in PATTERNS and key not in priorities:priorities.append(key)
    buffer=''

    def consume(value,level,kind=None):
        nonlocal buffer
        if len(value)<=limit:
            if buffer and len(buffer)+len(value)>limit:
                if kind=='paragraph' and level<len(priorities):
                    key=priorities[level];start=0
                    for match in re.finditer(PATTERNS[key],value):
                        end=match.end()
                        if end>start:yield from consume(value[start:end],level+1,key)
                        start=end
                    if start<len(value):yield from consume(value[start:],level+1,key)
                    return
                yield buffer;buffer=''
            buffer+=value
            return
        if kind=='sentence' and buffer:
            yield buffer;buffer=''
        if level<len(priorities):
            key=priorities[level];start=0
            for match in re.finditer(PATTERNS[key],value):
                end=match.end()
                if end>start:yield from consume(value[start:end],level+1,key)
                start=end
            if start<len(value):yield from consume(value[start:],level+1,key)
        else:
            # Mandatory hard bound when no permitted delimiter exists.
            if buffer:yield buffer;buffer=''
            for start in range(0,len(value),limit):
                part=value[start:start+limit]
                if len(part)==limit:yield part
                else:buffer=part

    def finish(part):
        return re.sub(r' *\n+ *', ' ', part) if options.get('remove_linebreaks', False) else part

    for part in consume(text,0):
        yield finish(part)
    if buffer:yield finish(buffer)
