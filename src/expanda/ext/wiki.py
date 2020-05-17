import os
import re
import bz2
import shutil
import mwparserfromhell as mw
import xml.etree.cElementTree as etree
from typing import List, Dict, Any
from multiprocessing import Process, Queue


def _clean_wiki_text(code: str, ns: List[str] = []):
    # Parse wiki code by using `mwparserfromhell` and create namespace-based
    # wiki-link pattern.the
    wiki = mw.parse(code)
    regex_mw_nslinks = re.compile('^(?:{}):'.format('|'.join(ns)),
                                  re.IGNORECASE)

    # Simple remove wrapper function.0
    def remove_element(section, obj):
        try:
            section.remove(obj)
        except ValueError:
            pass

    # Filter functions to remove from mediawiki code.
    def filter_wikilinks(obj):
        return bool(regex_mw_nslinks.match(str(obj.title)))

    def filter_templates(obj):
        return obj.name.lower() in {'reflist', 'notelist',
                                    'notelist-ua', 'notelist-lr',
                                    'notelist-ur', 'notelist-lg'}

    def filter_tags(obj):
        return str(obj.tag) in {'ref', 'table'}

    section_text = []
    for section in wiki.get_sections(flat=True,
                                     include_lead=True,
                                     include_headings=True):
        # Remove elements filtered by above functions.
        for obj in section.ifilter_headings(recursive=True):
            remove_element(section, obj)
        for obj in section.ifilter_wikilinks(matches=filter_wikilinks,
                                             recursive=True):
            remove_element(section, obj)
        for obj in section.ifilter_templates(matches=filter_templates,
                                             recursive=True):
            remove_element(section, obj)
        for obj in section.ifilter_tags(matches=filter_tags, recursive=True):
            remove_element(section, obj)

        # Add cleaned wiki contents to list.
        section_text.append(section.strip_code().strip())

    # Post-process cleaned wiki article content through simple sentence
    # testing.
    filtered = []
    for text in section_text:
        for line in text.strip().splitlines():
            # Check if text has normal punctuation.
            ordinary = False
            for punct in '!?.':
                if line.endswith(punct):
                    ordinary = True
                    break

            if ordinary:
                filtered.append(line)

    return '\n'.join(filtered)


def _process_article_worker(output_file: str, ns: List[str], queue: Queue):
    file = open(output_file, 'w', encoding='utf-8')

    while True:
        code = queue.get()
        if code is None:
            break

        # Write cleaned wiki articles into the output file.
        file.write(_clean_wiki_text(code, ns))

    file.close()


def _tokenize_sentences_worker(input_file: str, output_file: str,
                               temporary: str, lang: str, min_len: int):
    if lang == 'en':
        import nltk

        # Download nltk resources into `temporary` directory.
        os.environ['NLTK_DATA'] = temporary
        try:
            nltk.data.find('tokenizers/punkt')
        except LookupError:
            nltk.download('punkt')

        tokenize_sentence = nltk.tokenize.sent_tokenize
    elif lang == 'ko':
        import kss
        tokenize_sentence = kss.split_sentences
    else:
        raise NotImplementedError(f'language [{lang}] is not supported.')

    with open(input_file, 'r', encoding='utf-8') as src, \
            open(output_file, 'w', encoding='utf-8') as dst:
        for line in src:
            if len(line) < min_len:
                continue

            sentences = tokenize_sentence(line)
            dst.write('\n'.join([s for s in sentences if s]) + '\n')


def _extract_wiki_corpus(input_file: str, output_file: str, temporary: str,
                         args: Dict[str, Any]):
    # Open wikipedia dump file.
    file = bz2.open(input_file, 'r')
    context = etree.iterparse(file, events=('start', 'end'))

    # Get language code.
    _, root = next(context)
    for name, value in root.items():
        if name.endswith('lang'):
            lang = value
            break

    # Collect namespaces.
    ns = []
    for event, elem in context:
        if event != 'end':
            continue

        if elem.tag.endswith('namespace'):
            if elem.text is not None:
                ns.append(elem.text)

        # Exit the loop if it is finished to read `<namespaces>` tag.
        if elem.tag.endswith('namespaces'):
            break

    # Start extracting processes.
    workers = []
    queue = Queue(maxsize=10 * args['num-cores'])

    for i in range(args['num-cores']):
        w = Process(target=_process_article_worker,
                    args=(os.path.join(temporary, f'wiki{i}'), ns, queue))
        w.daemon = True
        w.start()
        workers.append(w)

    # Parse articles from dump file and put into the queue.
    regex_prefix = re.compile(r'({.*?})')
    for event, elem in context:
        if event != 'end' or not elem.tag.endswith('page'):
            continue

        # Skip the article which does not have namespace of 0.
        prefix = regex_prefix.match(elem.tag).group(0)
        if elem.find(f'./{prefix}ns').text != '0':
            root.clear()
            continue

        # Read raw wiki code of article content from the element.
        article = elem.find(f'./{prefix}revision/{prefix}text').text
        root.clear()

        # Skip empty or redirection articles.
        if article is None or article.lower().startswith('#redirect'):
            continue

        # Add to the queue.
        queue.put(article)

    # Finish the workers and wait for joining.
    for _ in range(args['num-cores']):
        queue.put(None)
    for w in workers:
        w.join()

    # Start splitting processes.
    workers = []
    for i in range(args['num-cores']):
        os.makedirs(os.path.join(temporary, f'tmp{i}'))

        w = Process(target=_tokenize_sentences_worker,
                    args=(os.path.join(temporary, f'wiki{i}'),
                          os.path.join(temporary, f'split{i}'),
                          os.path.join(temporary, f'tmp{i}'),
                          lang,
                          args['min-length']))
        w.daemon = True
        w.start()

        workers.append(w)

    # Wait for joining.
    for w in workers:
        w.join()
    for i in range(args['num-cores']):
        os.remove(os.path.join(temporary, f'wiki{i}'))
        shutil.rmtree(os.path.join(temporary, f'tmp{i}'))

    # Merge them into `output_file`.
    with open(output_file, 'wb') as dst:
        for i in range(args['num-cores']):
            with open(os.path.join(temporary, f'split{i}'), 'rb') as src:
                shutil.copyfileobj(src, dst)

    # Cleanup temporary files.
    for i in range(args['num-cores']):
        os.remove(os.path.join(temporary, f'split{i}'))


__extension__ = {
    'name': 'wikipedia dump extractor',
    'version': '1.0',
    'description': 'extract wiki dump file.',
    'author': 'expanda',
    'main': _extract_wiki_corpus,
    'arguments': {
        'num-cores': {'type': int, 'default': -1},
        'min-length': {'type': int, 'default': 50}
    }
}