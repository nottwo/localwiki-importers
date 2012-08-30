# coding=utf-8
import os
import site
import sys

if "DJANGO_SETTINGS_MODULE" not in os.environ:
    print "This importer must be run from the manage.py script"
    sys.exit(1)

import time
import hashlib
import html5lib
from lxml import etree

_treebuilder = html5lib.treebuilders.getTreeBuilder("lxml")

from xml.dom import minidom
from urlparse import urljoin, urlsplit, urlparse, parse_qs
import urllib
import re
from dateutil.parser import parse as date_parse
from mediawikitools import *

from django.db import transaction
from django.db import IntegrityError, connection
from pages.plugins import unquote_url
from django.db.utils import IntegrityError

from coord_finder import find_non_googlemaps_coordinates

_maps_installed = False
try:
    import maps.models
    _maps_installed = True
except ImportError:
    pass


class mediawiki:


    def __init__(self, url):
        self.site = wiki.Wiki(self.guess_api_endpoint(url))
        self.SCRIPT_PATH = self.guess_script_path(url)
        self.include_pages_to_create = []
        self.mapdata_objects_to_create = []

    def guess_api_endpoint(self, url):
        return urljoin(url, 'api.php')


    def guess_script_path(self, url):
        mw_path = urlsplit(url).path
        if mw_path.endswith('.php'):
            return mw_path
        if not mw_path:
            return '/'
        return urljoin(mw_path, '.')


    def set_script_path(self, path):
        self.SCRIPT_PATH = path


    def process_concurrently(self, work_items, work_func, num_workers=1, name='items'):
        """ Apply a function to all work items using a number of concurrent workers
        """
        from Queue import Queue
        from threading import Thread
        import traceback

        q = Queue()
        for item in work_items:
            q.put(item)

        num_items = q.qsize()

        def worker():
            while True:
                items_left = q.qsize()
                progress = 100 * (num_items - items_left) / num_items
                print "%d %s left to process (%d%% done)" % (items_left, name,
                                                             progress)
                item = q.get()
                try:
                    work_func(item)
                except:

                    traceback.print_exc()
                    "Unable to process %s" % item
                q.task_done()

        for i in range(num_workers):
             t = Thread(target=worker)
             t.daemon = True
             t.start()
        # wait for all workers to finish
        q.join()


    def get_robot_user(self):
        from django.contrib.auth.models import User
        try:
            u = User.objects.get(username="LocalWikiRobot")
        except User.DoesNotExist:
            u = User(name='LocalWiki Robot', username='LocalWikiRobot',
                     email='editrobot@localwiki.org')
            u.save()
        return u


    def import_users(self):
        from django.contrib.auth.models import User

        request = api.APIRequest(self.site, {
            'action': 'query',
            'list': 'allusers',
            'aulimit': 500,
        })
        for item in request.query()['query']['allusers']:
            username = item['name'][:30]

            # TODO: how do we get their email address here? I don't think
            # it's available via the API. Maybe we'll have to fill in the
            # users' emails in a separate step.
            # We require users to have an email address, so we fill this in with a
            # dummy value for now.
            name_hash = hashlib.sha1(username.encode('utf-8')).hexdigest()
            email = "%s@FIXME.localwiki.org" % name_hash

            if User.objects.filter(username=username).exists():
                continue

            print "Importing user %s" % username.encode('utf-8')
            u = User(username=username, email=email)
            u.save()


    def fix_pagename(self, name):
        if name.startswith('Talk:'):
            return name[5:] + "/Talk"
        if name.startswith('User:'):
            return "Users/" + name[5:]
        if name.startswith('User talk:'):
            return "Users/" + name[10:] + "/Talk"
        if name.startswith('Category:'):
            # For now, let's just throw these into the main
            # namespace.  TODO: Convert to tags.
            return name[9:]
        if name.startswith('Category talk:'):
            # For now, let's just throw these into the main
            # namespace.  TODO: Convert to tags.
            return name[14:] + "/Talk"
        return name


    def import_redirect(self, from_pagename):
        # We create the Redirects here.  We don't try and port over the
        # version information for the formerly-page-text-based redirects.
        to_pagename = self.parse_redirect(from_pagename)
        if to_pagename is None:
            print "Error creating redirect: %s has no link" % from_pagename
            return
        to_pagename = self.fix_pagename(to_pagename)

        from pages.models import Page, slugify
        from redirects.models import Redirect

        u = self.get_robot_user()

        try:
            to_page = Page.objects.get(slug=slugify(to_pagename))
        except Page.DoesNotExist:
            print "Error creating redirect: %s --> %s" % (
                from_pagename.encode('utf-8'), to_pagename.encode('utf-8'))
            print "  (page %s does not exist)" % to_pagename.encode('utf-8')
            return

        if slugify(from_pagename) == to_page.slug:
            return
        if not Redirect.objects.filter(source=slugify(from_pagename)):
            r = Redirect(source=slugify(from_pagename), destination=to_page)
        try:
            r.save(user=u, comment="Automated edit. Creating redirect.")
        except IntegrityError:
            connection.close()
        print "Redirect %s --> %s created" % (from_pagename.encode('utf-8'), to_pagename.encode('utf-8'))

    def import_redirects(self):
        redirects = [mw_p.title for mw_p in self.get_redirects()]
        self.process_concurrently(redirects, self.import_redirect,
                             num_workers=4, name='redirects')


    def process_mapdata(self):
        # We create the MapData models here.  We can't create them until the
        # Page objects are created.

        from maps.models import MapData
        from pages.models import Page, slugify
        from django.contrib.gis.geos import Point, MultiPoint

        for item in self.mapdata_objects_to_create:
            page_name = item['pagename'].encode('utf-8')
            print "Adding mapdata for", page_name
            try:
                p = Page.objects.get(slug=slugify(item['pagename']))
            except Page.DoesNotExist:
                print "*** Warning *** Skipping mapdata for page", page_name
                print "    Found mapdata for the page on wikimedia site, but the page does not exist in localwiki."
                continue

            mapdata = MapData.objects.filter(page=p)
            y = float(item['lat'])
            x = float(item['lon'])
            point = Point(x, y)
            if mapdata:
                m = mapdata[0]
                points = m.points
                points.append(point)
                m.points = points
            else:
                points = MultiPoint(point)
                m = MapData(page=p, points=points)
            try:
                m.save()
            except IntegrityError:
                connection.close()


    def parse_page(self, site, page_name):
        """
        Attrs:
            page_name: Name of page to render.

        Returns:
            Dictionary containing:
             "html" - HTML string of the rendered wikitext
             "links" - List of links in the page
             "templates" - List of templates used in the page
             "categories" - List of categories
        """
        request = api.APIRequest(site, {
            'action': 'parse',
            'page': page_name
        })
        return self.parse_result(request)


    def parse_revision(self, rev_id):
        """
        Attrs:
            rev_id: Revision to render.

        Returns:
            Dictionary containing:
             "html" - HTML string of the rendered wikitext
             "links" - List of links in the page
             "templates" - List of templates used in the page
             "categories" - List of categories
        """
        request = api.APIRequest(self.site, {
            'action': 'parse',
            'oldid': rev_id
        })
        return self.parse_result(request)


    def parse_redirect(self, page_name):
        """
        Attrs:
            page_name: Name of redirect page to parse

        Returns:
            Redirect destination link or None
        """
        request = api.APIRequest(self.site, {
            'action': 'parse',
            'page': page_name,
            'prop': 'links'
        })
        result = self.parse_result(request)
        if result["links"]:
            return result["links"][0]
        return None


    def parse_result(self, request):
        result = request.query()['parse']

        parsed = {}
        html = result.get('text', None)
        if html:
            parsed["html"] = result['text']['*']
        links = result.get('links', [])
        parsed["links"] = [l['*'] for l in links]
        templates = result.get('templates', [])
        parsed["templates"] = [t['*'] for t in templates]
        categories = result.get('categories', [])
        parsed["categories"] = [c['*'] for c in categories]
        return parsed


    def parse_wikitext(self, wikitext, title):
        """
        Attrs:
            wikitext: Wikitext to parse.
            title: Title with which to render the page.

        Returns:
            HTML string of the parsed wikitext
        """
        request = api.APIRequest(self.site, {
            'action': 'parse',
            'text': wikitext,
            'title': title
        })
        result = request.query()['parse']
        return result['text']['*']


    def _convert_to_string(self, l):
        s = ''
        for e in l:
            # ignore broken elements and HTML comments
            if e is None or isinstance(e, etree._Comment):
                continue
            if type(e) == str:
                s += e
            elif type(e) == unicode:
                s += e.encode('utf-8')
            elif isinstance(e, list):
                s += self._convert_to_string(e)
            else:
                s += etree.tostring(e, method='html', encoding='UTF-8')
        return s.decode('utf-8')


    def _is_wiki_page_url(self, href):
        if self.SCRIPT_PATH and href.startswith(self.SCRIPT_PATH):
            return True
        else:
            split_url = urlsplit(href)
            # If this is a relative url and has 'index.php' in it we'll say
            # it's a wiki link.
            if not split_url.scheme and split_url.path.endswith('index.php'):
                return True
        return False


    def _get_wiki_link(self, link):
        """
        If the provided link is a wiki link then we return the name of the
        page to link to.  If it's not a wiki link then we return None.
        """
        pagename = None
        if 'href' in link.attrib:
            href = link.attrib['href']
            if self._is_wiki_page_url(href):
                title = link.attrib.get('title')
                if 'new' in link.attrib.get('class', '').split():
                    # It's a link to a non-existent page, so we parse the
                    # page name from the title attribute in a really
                    # hacky way.  Titles for non-existent links look
                    # like <a ... title="Page name (page does not exist)">
                    pagename = title[:title.rfind('(') - 1]
                else:
                    pagename = title
        if type(pagename) == unicode:
            pagename = pagename.encode('utf-8')

        if pagename:
            pagename = self.fix_pagename(pagename)

        return pagename


    def fix_internal_links(self, tree):
        def _process(item):
            pagename = self._get_wiki_link(item)
            if pagename:
                # Set href to quoted pagename and clear out other attributes
                for k in item.attrib:
                    del item.attrib[k]
                item.attrib['href'] = urllib.quote(pagename)

        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            if elem.tag == 'a':
                _process(elem)
            for link in elem.findall('.//a'):
                _process(link)
        return tree


    def fix_basic_tags(self, tree):
        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            # Replace i, b with em, strong.
            if elem.tag == 'b':
                elem.tag = 'strong'
            for item in elem.findall('.//b'):
                item.tag = 'strong'

            if elem.tag == 'i':
                elem.tag = 'em'
            for item in elem.findall('.//i'):
                item.tag = 'em'

            # Replace <big> with <strong>
            if elem.tag == 'big':
                elem.tag = 'strong'
            for item in elem.findall('.//big'):
                item.tag = 'strong'

            # Replace <font> with <strong>
            if elem.tag == 'font':
                elem.tag = 'strong'
            for item in elem.findall('.//font'):
                item.tag = 'strong'

            # Replace <code> with <tt>
            if elem.tag == 'code':
                elem.tag = 'tt'
            for item in elem.findall('.//code'):
                item.tag = 'tt'

        return tree


    def remove_edit_links(self, tree):
        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            if (elem.tag == 'span' and
                ('editsection' in elem.attrib.get('class').split())):
                elem.tag = 'removeme'
            for item in elem.findall(".//span[@class='editsection']"):
                item.tag = 'removeme'  # hack to easily remove a bunch of elements
        return tree


    def throw_out_tags(self, tree):
        throw_out = ['small']
        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            for parent in elem.getiterator():
                for child in parent:
                    if (child.tag in throw_out):
                        parent.text = parent.text or ''
                        parent.tail = parent.tail or ''
                        if child.text:
                            parent.text += (child.text + (child.tail or ''))
                        child.tag = 'removeme'
        return tree


    def remove_headline_labels(self, tree):
        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            for parent in elem.getiterator():
                for child in parent:
                    if (child.tag == 'span' and
                        'mw-headline' in child.attrib.get('class', '').split()):
                        parent.text = parent.text or ''
                        parent.tail = parent.tail or ''
                        if child.text:
                            # We strip() here b/c mediawiki pads the text with a
                            # space for some reason.
                            tail = child.tail or ''
                            parent.text += (child.text.strip() + tail)
                        child.tag = 'removeme'
        return tree


    def remove_elements_tagged_for_removal(self, tree):
        new_tree = []
        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            if elem.tag == 'removeme':
                continue
            for parent in elem.getiterator():
                for child in parent:
                    if child.tag == 'removeme':
                        parent.remove(child)
            new_tree.append(elem)
        return new_tree


    def _get_templates_on_page(self, pagename):
        params = {
            'action': 'query',
            'prop': 'templates',
            'titles': pagename,
        }
        req = api.APIRequest(self.site, params)
        response = req.query()
        pages = response['query']['pages']

        if not pages:
            return []

        page_info = pages[pages.keys()[0]]
        if not 'templates' in page_info:
            return []

        # There are some templates in use.
        return [e['title'] for e in page_info['templates']]


    def _render_template(self, template_name, page_title=None):
        if page_title is None:
            page_title = template_name
        name_part = template_name[len('Template:'):]
        wikitext = '{{%s}}' % name_part
        html = self.parse_wikitext(wikitext, page_title)
        return html


    def create_mw_template_as_page(self, template_name, template_html):
        """
        Create a page to hold the rendered template.

        Returns:
            String representing the pagename of the new include-able page.
        """
        from pages.models import Page, slugify

        robot = self.get_robot_user()

        name_part = template_name[len('Template:'):]
        # Keeping it simple for now.  We can namespace later if people want that.
        include_name = name_part

        if not Page.objects.filter(slug=slugify(include_name)):
            mw_page = page.Page(self.site, title=template_name)
            p = Page(name=include_name)
            p.content = self.process_html(template_html, pagename=template_name,
                                     mw_page_id=mw_page.pageid,
                                     attach_img_to_pagename=include_name,
                                     show_img_borders=False)
            p.clean_fields()
            # check if it exists again, processing takes time
            if not Page.objects.filter(slug=slugify(include_name)):
                p.save(user=robot, comment="Automated edit. Creating included page.")

        return include_name


    def replace_mw_templates_with_includes(self, tree, templates, page_title):
        """
        Replace {{templatethings}} inside of pages with our page include plugin.

        We can safely do this when the template doesn't have any arguments.
        When it does have arguments we just import it as raw HTML for now.
        """
        # We use the API to figure out what templates are being used on a given
        # page, and then translate them to page includes.  This can be done for
        # templates without arguments.
        #
        # The API doesn't tell us whether or not a template has arguments,
        # but we can figure this out by rendering the template and comparing the
        # resulting HTML to the HTML inside the rendered page.  If it's identical,
        # then we know we can replace it with an include.

        def _normalize_html(s):
            p = html5lib.HTMLParser(tokenizer=html5lib.tokenizer.HTMLTokenizer,
                tree=_treebuilder,
                namespaceHTMLElements=False)
            tree = p.parseFragment(s, encoding='UTF-8')
            return self._convert_to_string(tree)

        # Finding and replacing is easiest if we convert the tree to
        # HTML and then back again.  Maybe there's a better way?

        html = self._convert_to_string(tree)
        for template in templates:
            normalized = _normalize_html(self._render_template(template, page_title))
            template_html = normalized.strip()
            if template_html and template_html in html:
                # It's an include-style template.
                include_pagename = self.create_mw_template_as_page(template,
                    template_html)
                include_classes = ''
                include_html = (
                    '<a href="%(quoted_pagename)s" '
                     'class="plugin includepage%(include_classes)s">'
                     'Include page %(pagename)s'
                    '</a>' % {
                        'quoted_pagename': urllib.quote(include_pagename),
                        'pagename': include_pagename,
                        'include_classes': include_classes,
                        }
                )
                html = html.replace(template_html, include_html)

        p = html5lib.HTMLParser(tokenizer=html5lib.tokenizer.HTMLTokenizer,
                tree=_treebuilder,
                namespaceHTMLElements=False)
        tree = p.parseFragment(html, encoding='UTF-8')
        return tree


    def fix_googlemaps(self, tree, pagename, save_data=True):
        """
        If the googlemaps extension is installed, then we process googlemaps here.

        If the googlemaps extension isn't installed but its markup is in the wiki
        then the maps get processed in process_non_html_elements.
        """
        def _parse_mapdata(elem):
            if not save_data:
                return
            img = elem.find('.//img')
            if img is None:
                return
            src = img.attrib.get('src')
            center = parse_qs(urlparse(src).query)['center']
            lat, lon = center[0].split(',')
            d = {'pagename': pagename, 'lat': lat, 'lon': lon}
            self.mapdata_objects_to_create.append(d)

        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            if elem.tag == 'div' and elem.attrib.get('id', '').startswith('map'):
                _parse_mapdata(elem)
                elem.tag = 'removeme'
                continue
            for item in elem.findall(".//div"):
                if item.attrib.get('id', '').startswith('map'):
                    _parse_mapdata(item)
                    item.tag = 'removeme'

        return tree


    def fix_embeds(self, tree):
        """
        Replace <object>-style embeds with <iframe> for stuff we know how to work
        with.
        """
        def _parse_flow_player(s):
            query = parse_qs(urlparse(s).query)
            config = query.get('config', None)
            if not config:
                return ''
            config = config[0]
            if 'url:' not in config:
                return ''
            video_id = config.split("url:'")[1].split('/')[0]
            return 'http://www.archive.org/embed/%s' % video_id

        def _fix_embed(elem):
            iframe = etree.Element('iframe')
            if 'width' in elem.attrib:
                iframe.attrib['width'] = elem.attrib['width']
            if 'height' in elem.attrib:
                iframe.attrib['height'] = elem.attrib['height']
            movie = elem.find('.//param[@name="movie"]')
            if movie is None:
                return
            moviestr = movie.attrib['value']
            if moviestr.startswith('http://www.archive.org/flow/'):
                iframe.attrib['src'] = _parse_flow_player(moviestr)

            elem.clear()
            elem.tag = 'span'
            elem.attrib['class'] = "plugin embed"
            elem.text = self._convert_to_string([iframe])

        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            if elem.tag == 'object':
                _fix_embed(elem)
                continue
            for item in elem.findall(".//object"):
                _fix_embed(item)
        return tree


    def fix_references(self, tree):
        """
        Replace <li id="cite_blah"> with <li><a name="cite_blah"></a>
        """

        def _fix_reference(elem):
            if 'id' not in elem.attrib:
                return
            text = elem.text or ''
            elem.text = ''
            # remove arrow up thing
            if len(text) and text[0] == u"\u2191":
                text = text[1:]
            # remove back-links to citations
            for item in elem.findall(".//a[@href]"):
                if item.attrib['href'].startswith('#'):
                    parent = item.getparent()
                    if parent.tag == 'sup':
                        text += parent.tail or ''
                        parent.getparent().remove(parent)
                    else:
                        text += item.tail or ''
                        parent.remove(item)
            # create anchor
            anchor = etree.Element('a')
            anchor.attrib['name'] = elem.attrib['id']
            elem.insert(0, anchor)
            anchor.tail = text.lstrip()

        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            if elem.tag == 'li':
                _fix_reference(elem)
                continue
            for item in elem.findall(".//li"):
                _fix_reference(item)
        return tree


    def process_non_html_elements(self, html, pagename):
        """
        Some MediaWiki extensions (e.g. google maps) output custom tags like
        &lt;googlemap&gt;.  We process those here.
        """
        def _repl_googlemap(match):
            xml = '<googlemap %s></googlemap>' % match.group('attribs')
            try:
                dom = minidom.parseString(xml)
            except:
                return ''
            elem = dom.getElementsByTagName('googlemap')[0]
            lon = elem.getAttribute('lon')
            lat = elem.getAttribute('lat')

            d = {'pagename': pagename, 'lat': lat, 'lon': lon}
            self.mapdata_objects_to_create.append(d)

            return ''  # Clear out the googlemap tag nonsense.

        html = re.sub(
            '(?P<map>&lt;googlemap (?P<attribs>.+?)&gt;'
                '((.|\n)+?)'
            '&lt;/googlemap&gt;)',
            _repl_googlemap, html)
        return html





    def fix_image_html(self, mw_img_title, quoted_mw_img_title, filename, tree,
            border=True):
        # Images start with something like this:
        # <a href="/mediawiki-1.16.0/index.php/File:1009-Packard.jpg"><img
        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            for img_a in elem.findall(".//a[@href]"):
                if img_a.find(".//img") is None:
                    continue
                href = unquote_url(img_a.attrib.get('href', 'no href')) 
                if href.endswith(quoted_mw_img_title):
                    # This is a link to the image with class image, so this is an
                    # image reference.

                    # Let's turn the image's <a> tag into the <span> tag with
                    # an <img> inside it.  And set all the attributes to the
                    # correct values.
                    # Our images look like this:
                    # <span class="image_frame image_frame_border">
                    #    <img src="_files/narwals.jpg"
                    #         style="width: 272px; height: 362px;">
                    # </span>
                    if border:
                        extra_classes = ' image_frame_border'
                    else:
                        extra_classes = ''
                    img_elem = img_a.find('img')
                    width = img_elem.attrib.get('width')
                    height = img_elem.attrib.get('height')
                    is_thumb = 'thumbimage' in img_elem.attrib.get('class', '')
                    caption = None
                    if is_thumb:
                        img_wrapper = img_a.getparent().getparent()
                    else:
                        # Is this a floated, non-thumbnailed image
                        if (img_a.getparent() is not None and
                            'float' in img_a.getparent().attrib.get('class', '')):
                            img_wrapper = img_a.getparent()
                        else:
                            img_wrapper = img_a

                    if is_thumb:
                        # We use the parent's class info to figure out whether to
                        # float the image left/right.
                        #
                        # The MediaWiki HTML looks like this:
                        #
                        # <div class="thumb tright">
                        #   <div class="thumbinner" style="width:302px;">
                        #     <a href="/index.php/File:Michigan-State-Telephone-Company.png" class="image">
                        #       <img alt="" src="/mediawiki-1.16.0/images/thumb/d/dd/Michigan-State-Telephone-Company.png/300px-Michigan-State-Telephone-Company.png" width="300" height="272" class="thumbimage" />
                        #     </a>
                        #     <div class="thumbcaption">
                        #        <div class="magnify"><a href="/mediawiki-1.16.0/index.php/File:Michigan-State-Telephone-Company.png" class="internal" title="Enlarge"><img src="/mediawiki-1.16.0/skins/common/images/magnify-clip.png" width="15" height="11" alt="" /></a>
                        #        </div>
                        #        <strong class="selflink">Michigan State Telephone Company</strong>
                        #     </div>
                        #   </div>
                        # </div>
                        if 'tright' in img_wrapper.attrib.get('class'):
                            extra_classes += ' image_right'
                        elif 'tleft' in img_wrapper.attrib.get('class'):
                            extra_classes += ' image_left'
                        # Does the image have a caption?
                        caption = img_wrapper.find(".//div[@class='thumbcaption']")
                        if caption is not None:
                            magnify = caption.find(".//div[@class='magnify']")
                            tail = ''
                            if magnify is not None:
                                tail = magnify.tail
                                caption.remove(magnify)
                            if tail:
                                caption.text = caption.text or ''
                                caption.text += tail
                            # MediaWiki creates a caption div even if the
                            # image doesn't have a caption, so we have to
                            # test to see if the div is empty here.
                            if not (self._convert_to_string(caption) or caption.text):
                                # No caption content, so let's set caption
                                # to None.
                                caption = None
                            # Caption is now clean.  Yay!
                    else:
                        # Can still be floated
                        if 'floatright' in img_wrapper.attrib.get('class', ''):
                            extra_classes += ' image_right'
                        elif 'floatright' in img_wrapper.attrib.get('class', ''):
                            extra_classes += ' image_left'

                    img_wrapper.clear()
                    img_wrapper.tag = 'span'

                    img_wrapper.attrib['class'] = (
                        'image_frame' + extra_classes)
                    img = etree.Element("img")
                    img.attrib['src'] = "_files/%s" % filename
                    if width and height:
                        img.attrib['style'] = 'width: %spx; height: %spx;' % (
                            width, height
                        )
                    img_wrapper.append(img)
                    if caption is not None:
                        caption.tag = 'span'
                        caption.attrib['class'] = 'image_caption'
                        caption.attrib['style'] = 'width: %spx;' % width
                        img_wrapper.append(caption)

        return tree


    def page_url_to_name(self, page_url):
        # Some wikis use pretty urls and some use ?title=
        if '?title=' in page_url:
            return page_url.split('?title=')[1]
        return urlsplit(page_url).path.split('/')[-1]


    def get_image_info(self, image_title):
        params = {
                'prop': 'imageinfo',
                'action': 'query',
                'titles': image_title,
                'iiprop': 'timestamp|user|url|dimensions|comment',
                'imlimit': 500,
            }
        req = api.APIRequest(self.site, params)
        response = req.query()
        info_by_pageid = response['query']['pages']
        # Doesn't matter what page it's on, we just want the info.
        info = info_by_pageid[info_by_pageid.keys()[0]]
        return info['imageinfo'][0]


    def grab_images(self, tree, page_id, pagename, attach_to_pagename=None,
            show_image_borders=True):
        """
        Imports the images on a page as PageFile objects and fixes the page's
        HTML to be what we want for images.
        """
        from django.core.files.base import ContentFile
        from pages.models import slugify, PageFile

        robot = self.get_robot_user()

        # Get the list of images on this page
        params = {
            'action': 'query',
            'prop': 'images',
            'imlimit': 500,
            'pageids': page_id,
        }
        req = api.APIRequest(self.site, params)
        response = req.query()
        imagelist_by_pageid = response['query']['pages']
        # We're processing one page at a time, so just grab the first.
        imagelist = imagelist_by_pageid[imagelist_by_pageid.keys()[0]]
        if not 'images' in imagelist:
            # Page doesn't have images.
            return tree
        images = imagelist['images']

        for image_dict in images:
            image_title = image_dict['title']
            filename = image_title[len('File:'):]
            # Get the image info for this image title
            try:
                image_info = self.get_image_info(image_title)
            except KeyError:
                # For some reason we can't get the image info.
                # TODO: Investigate this.
                continue
            image_url = image_info['url']
            image_description_url = image_info['descriptionurl']
            
            quoted_image_title = self.page_url_to_name(image_description_url)
            attach_to_pagename = attach_to_pagename or pagename

            if PageFile.objects.filter(name=filename,
                    slug=slugify(attach_to_pagename)):
                continue  # Image already exists.

            # For each image, find the image's supporting HTML in the tree
            # and transform it to comply with our HTML.
            html_before_fix = self._convert_to_string(tree)
            tree = self.fix_image_html(image_title, quoted_image_title, filename, tree,
                border=show_image_borders
            )

            if self._convert_to_string(tree) == html_before_fix:
                # Image isn't actually on the page, so let's not create or attach
                # the PageFile.
                continue

            # Get the full-size image binary and store it in a string.
            img_ptr = urllib.URLopener()
            img_tmp_f = open(img_ptr.retrieve(image_url)[0], 'r')
            file_content = ContentFile(img_tmp_f.read())
            img_tmp_f.close()
            img_ptr.close()

            # Create the PageFile and associate it with the current page.
            print "Creating image %s on page %s" % (filename, pagename.encode('utf-8'))
            try:
                pfile = PageFile(name=filename, slug=slugify(attach_to_pagename))
                pfile.file.save(filename, file_content, save=False)
                pfile.save(user=robot, comment="Automated edit. Creating file.")
            except IntegrityError:
                connection.close()

        return tree


    def fix_indents(self, tree):
        def _change_to_p():
            # We replace the dl_parent with the dd_item
            dl_parent.clear()
            dl_parent.tag = 'p'
            dl_parent.attrib['class'] = 'indent%s' % depth
            for child in dd_item.iterchildren():
                dl_parent.append(child)
            dl_parent.text = dl_parent.text or ''
            dl_parent.text += (dd_item.text or '')
            dl_parent.tail = dl_parent.tail or ''
            dl_parent.tail += (dd_item.tail or '')
        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            in_dd = False
            depth = 0
            for item in elem.iter():
                if item is None:
                    continue
                if item.tag == 'dl' and not in_dd:
                    dl_parent = item
                if item.tag == 'dd':
                    depth += 1
                    in_dd = True
                    dd_item = item
                if in_dd and item.tag not in ('dd', 'dl'):
                    in_dd = False
                    _change_to_p()
            if in_dd:
                # Ended in dd
                _change_to_p()
        return tree


    def remove_toc(self, tree):
        """
        Remove the table of contents table.
        """
        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            if elem.tag == 'table' and elem.attrib.get('id') == 'toc':
                elem.tag = 'removeme'
            toc = elem.find(".//table[@id='toc']")
            if toc is not None:
                toc.tag = 'removeme'
        return tree


    def remove_script_tags(self, html):
        """
        Remove script tags.
        """
        return re.sub('<script(.|\n)*?>(.|\n)*?<\/script>', '', html)


    def replace_blockquote(self, tree):
        """
        Replace <blockquote> with <p class="indent1">
        """
        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            if elem.tag == 'blockquote':
                elem.tag = 'p'
                elem.attrib['class'] = 'indent1'
            for item in elem.findall(".//blockquote"):
                item.tag = 'p'
                item.attrib['class'] = 'ident1'
        return tree


    def fix_image_galleries(self, tree):
        """
        We remove the image gallery wrapper HTML / table and we move the
        gallery text caption into the image caption itself.

        At some point we may have our own 'gallery' mode for displaying a set
        of images at equal size, but for now we just run them all together - it
        should look pretty reasonable in most cases.
        """
        def _fix_gallery(item):
            # Grab all of the image spans inside of the item table.
            p = etree.Element("p")
            for image in item.findall(".//span"):
                if not 'image_frame' in image.attrib.get('class'):
                    continue
                caption = image.getparent().getparent(
                    ).getparent().find(".//div[@class='gallerytext']")
                # We have a gallery caption, so let's add it to our image
                # span.
                if caption is not None:
                    img_style = image.find('img').attrib['style']
                    for css_prop in img_style.split(';'):
                        if css_prop.startswith('width:'):
                            width = css_prop
                    our_caption = etree.Element("span")
                    our_caption.attrib['class'] = 'image_caption'
                    our_caption.attrib['style'] = '%s;' % width
                    # Caption has an inner p, and we don't want that.
                    caption_p = caption.find('p')
                    if caption_p is not None:
                        caption = caption_p
                    for child in caption.iterchildren():
                        our_caption.append(child)
                    text = caption.text or ''
                    our_caption.text = text
                    image.append(our_caption)
                p.append(image)

            item.tag = 'removeme'
            if len(list(p.iterchildren())):
                return p
            return None

        new_tree = []
        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            if elem.tag == 'table' and elem.attrib.get('class') == 'gallery':
                gallery = _fix_gallery(elem)
                new_tree.append(gallery)
            else:
                for item in elem.findall(".//table[@class='gallery']"):
                    gallery = _fix_gallery(item)
                    pos = gallery.getparent().index()
                    gallery.getparent().insert(pos, gallery)
                    item.tag = 'removeme'
                new_tree.append(elem)

        return new_tree


    def convert_some_divs_to_tables(self, tree):
        """
        We don't allow generic <div>s.  So we convert some divs to table tags,
        which we allow styling on, aside from some special cases like addresses.
        """
        # For now we just convert divs to tables and let our HTML sanitization take
        # care of the rest.  This obviously won't always give the correct results,
        # but it's good enough most of the time. We convert special divs to span
        _special_classes = ['adr']
        def _fix(item):
            item_class = item.attrib.get('class', '')
            if any([c in _special_classes for c in item_class.split(' ')]):
                item.tag = 'span'
                return
            item.tag = 'table'
            tr = etree.Element('tr')
            td = etree.Element('td')
            tr.append(td)

            for child in item.iterchildren():
                td.append(child)
            td.text = item.text
            style = item.attrib.get('style')
            if style:
                td.attrib['style'] = style

            item.clear()
            item.append(td)

        for elem in tree:
            if elem is None or isinstance(elem, basestring):
                continue
            if elem.tag == 'div':
                _fix(elem)
            for item in elem.findall(".//div"):
                _fix(item)
        return tree


    def process_html(self, html, pagename=None, mw_page_id=None, templates=[],
            attach_img_to_pagename=None, show_img_borders=True, historic=False):
        """
        This is the real workhorse.  We take an html string which represents
        a rendered MediaWiki page and process bits and pieces of it, normalize
        elements / attributes and return cleaned up HTML.
        """

        # Find non-Google Maps geolocation coordinates
        coord = find_non_googlemaps_coordinates(html, pagename)
        if coord is not None:
            self.mapdata_objects_to_create.append(coord)
            print "Queueing mapdata for %s (Lat: %s, Lon: %s)" % (coord['page'],
                                                              coord['lat'], 
                                                              coord['lon']) 
    
        html = self.process_non_html_elements(html, pagename)
        html = self.remove_script_tags(html)
        p = html5lib.HTMLParser(tokenizer=html5lib.tokenizer.HTMLTokenizer,
                tree=_treebuilder,
                namespaceHTMLElements=False)
        tree = p.parseFragment(html, encoding='UTF-8')
        tree = self.replace_mw_templates_with_includes(tree, templates, pagename)
        tree = self.fix_references(tree)
        tree = self.fix_embeds(tree)
        tree = self.fix_googlemaps(tree, pagename, save_data=(not historic))
        tree = self.remove_elements_tagged_for_removal(tree)
        if pagename is not None and mw_page_id:
            tree = self.grab_images(tree, mw_page_id, pagename,
                attach_img_to_pagename, show_img_borders)
        tree = self.fix_internal_links(tree)
        tree = self.fix_basic_tags(tree)
        tree = self.remove_edit_links(tree)
        tree = self.remove_headline_labels(tree)
        tree = self.throw_out_tags(tree)
        tree = self.remove_toc(tree)
        tree = self.replace_blockquote(tree)
        tree = self.fix_image_galleries(tree)
        tree = self.fix_indents(tree)

        tree = self.convert_some_divs_to_tables(tree)

        tree = self.remove_elements_tagged_for_removal(tree)

        return self._convert_to_string(tree)


    def create_page_revisions(self, p, mw_p, parsed_page):
        from django.contrib.auth.models import User
        from pages.models import Page, slugify

        request = api.APIRequest(self.site, {
                'action': 'query',
                'prop': 'revisions',
                'rvprop': 'ids|timestamp|user|comment',
                'rvlimit': '500',
                'titles': mw_p.title,
        })
        response_pages = request.query()['query']['pages']
        first_pageid = response_pages.keys()[0]
        rev_num = 0
        total_revs = len(response_pages[first_pageid]['revisions'])
        for revision in response_pages[first_pageid]['revisions']:
            rev_num += 1
            if rev_num == total_revs:
                history_type = 0  # Added
            else:
                history_type = 1  # Updated

            history_comment = revision.get('comment', None)
            if history_comment:
                history_comment = history_comment[:200]

            username = revision.get('user', None)
            user = User.objects.filter(username=username)
            if user:
                user = user[0]
                history_user_id = user.id
            else:
                history_user_id = None
            history_user_ip = None  # MW offers no way to get this via API

            timestamp = revision.get('timestamp', None)
            history_date = date_parse(timestamp)

            revid = revision.get('revid', None)
            if rev_num == 1:  # latest revision is same as page
                parsed = parsed_page
            else:
                parsed = self.parse_revision(revid)
            html = parsed['html']

            # Create a dummy Page object to get the correct cleaning behavior
            dummy_p = Page(name=p.name, content=html)
            dummy_p.content = self.process_html(dummy_p.content, pagename=p.name,
                templates=parsed['templates'], mw_page_id=mw_p.pageid,
                historic=True)
            if not (dummy_p.content.strip()):
                dummy_p.content = '<p></p>'  # Can't be blank
            dummy_p.clean_fields()
            html = dummy_p.content

            p_h = Page.versions.model(
                id=p.id,
                name=p.name,
                slug=slugify(p.name),
                content=html,
                history_comment=history_comment,
                history_date=history_date,
                history_type=history_type,
                history_user_id=history_user_id,
                history_user_ip=history_user_ip
            )
            try:
                p_h.save()
            except IntegrityError:
                connection.close()
            print "Imported historical page %s" % p.name.encode('utf-8')


    def get_page_list(self, apfilterredir='nonredirects'):
        """ Returns a list of all pages in all namespaces. Exclude redirects by 
        default.
        """
        pages = []
        for namespace in ['0', '1', '2', '3', '14', '15']:
            request = api.APIRequest(self.site, {
                'action': 'query',
                'list': 'allpages',
                'aplimit': 500,
                'apnamespace': namespace,
                'apfilterredir': apfilterredir,
            })
            response_list = request.query()['query']['allpages']
            pages.extend(pagelist.listFromQuery(self.site, response_list))
        return pages


    def get_redirects(self):
        """ Returns a list of all redirect pages.
        """
        return self.get_page_list(apfilterredir='redirects')


    def import_page(self, mw_p):
        from pages.models import Page, slugify
        print "Importing %s" % mw_p.title.encode('utf-8')
        parsed = self.parse_page(self.site, mw_p.title)
        html = parsed['html']
        name = self.fix_pagename(mw_p.title)

        if Page.objects.filter(slug=slugify(name)).exists():
            print "Page %s already exists" % name
            # Page already exists with this slug.  This is probably because
            # MediaWiki has case-sensitive pagenames.
            other_page = Page.objects.get(slug=slugify(name))
            if len(html) > other_page.content:
                print "Clearing out other page..", other_page
                # *This* page has more content.  Let's use it instead.
                for other_page_version in other_page.versions.all():
                    other_page_version.delete()
                other_page.delete(track_changes=False)
            else:
                # Other page has more content.
                return

        if mw_p.title.startswith('Category:'):
            # include list of tagged pages
            include_html = (
                    '<a href="tags/%(quoted_tag)s" '
                     'class="plugin includetag includepage_showtitle">'
                     'List of pages tagged &quot;%(tag)s&quot;'
                    '</a>' % {
                        'quoted_tag': urllib.quote(name),
                        'tag': name,
                        }
                )
            html += include_html
        p = Page(name=name, content=html)
        p.content = self.process_html(p.content, pagename=p.name,
                                 templates=parsed['templates'],
                                 mw_page_id=mw_p.pageid, historic=False)

        if not (p.content.strip()):
            return  # page content can't be blank
        p.clean_fields()
        try:
            p.save(track_changes=False)
        except IntegrityError:
            connection.close()
        try:
            self.create_page_revisions(p, mw_p, parsed)
        except KeyError:
            # For some reason the response lacks a revisions key
            # TODO: figure out why
            pass
        self.process_page_categories(p, parsed['categories'])


    def import_pages(self):
        print "Getting master page list ..."
        self.get_robot_user() # so threads won't try to create one concurrently
        pages = self.get_page_list()
        self.process_concurrently(pages, self.import_page, num_workers=4, name='pages')


    def process_page_categories(self, page, categories):
        from tags.models import Tag, PageTagSet, slugify
        keys = []
        for c in categories:
            # MW uses underscores for spaces in categories
            c = str(c).replace("_", " ")
            try:
                tag, created = Tag.objects.get_or_create(slug=slugify(c),
                                                     defaults={'name': c})
                keys.append(tag.pk)
            except IntegrityError as e:
                pass
        if keys:
            pagetagset = PageTagSet.objects.create(page=page)
            pagetagset.tags = keys


    def clear_out_existing_data(self):
        """
        A utility function that clears out existing pages, users, files,
        etc before running the import.
        """
        from pages.models import Page, PageFile
        from redirects.models import Redirect
        from tags.models import Tag, PageTagSet

        for p in Page.objects.all():
            print 'Clearing out', p
            p.delete(track_changes=False)
            for p_h in p.versions.all():
                p_h.delete()

        for f in PageFile.objects.all():
            print 'Clearing out', f
            f.delete(track_changes=False)
            for f_h in f.versions.all():
                f_h.delete()

        for r in Redirect.objects.all():
            print 'Clearing out', r
            r.delete(track_changes=False)
            for r_h in r.versions.all():
                r_h.delete()

        for p in PageTagSet.objects.all():
            print 'Clearing out', p
            p.delete(track_changes=False)
            for p_h in p.versions.all():
                p_h.delete()

        for t in Tag.objects.all():
            print 'Clearing out', t
            t.delete(track_changes=False)
            for t_h in t.versions.all():
                t_h.delete()


def run():

    url = raw_input("Enter the address of a MediaWiki site (ex: http://arborwiki.org/): ")
    mw = mediawiki(url)
    sitename = mw.site.siteinfo.get('sitename', None)
    if not sitename:
        print "Unable to connect to API. Please check the address."
        sys.exit(1)
    print "Ready to import %s" % sitename

    yes_no = raw_input("This import will clear out any existing data in this "
                       "LocalWiki instance. Continue import? (yes/no) ")
    if yes_no.lower() != "yes":
        sys.exit()

    print "Clearing out existing data..."
    with transaction.commit_on_success():
        mw.clear_out_existing_data()
    start = time.time()
    print "Importing users..."
    with transaction.commit_on_success():
        mw.import_users()
    print "Importing pages..."
    mw.import_pages()
    print "Importing redirects..."
    mw.import_redirects()
    if _maps_installed:
        print "Processing map data..."
        mw.process_mapdata()
    print "Import completed in %.2f minutes" % ((time.time() - start) / 60.0)

if __name__ == '__main__':
    try:
        mediawiki.run()
    except KeyboardInterrupt:
        print  # just a newline
