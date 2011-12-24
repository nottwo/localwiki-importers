import hashlib
import html5lib
from lxml import etree
from urlparse import urljoin, urlsplit
import urllib
from html5lib import sanitizer
from wikitools import *

MEDIAWIKI_URL = 'http://127.0.0.1/mediawiki-1.16.0/index.php'


def guess_api_endpoint(url):
    return urljoin(url, 'api.php')


def guess_script_path(url):
    mw_path = urlsplit(MEDIAWIKI_URL).path
    if mw_path.endswith('.php'):
        return mw_path
    return urljoin(mw_path, '.')

API_ENDPOINT = guess_api_endpoint(MEDIAWIKI_URL)

site = wiki.Wiki(API_ENDPOINT)
SCRIPT_PATH = guess_script_path(MEDIAWIKI_URL)
redirects = []


def get_robot_user():
    from django.contrib.auth.models import User

    try:
        u = User.objects.get(username="LocalWikiRobot")
    except User.DoesNotExist:
        u = User(name='LocalWiki Robot', username='LocalWikiRobot',
                 email='editrobot@localwiki.org')
        u.save()
    return u


def import_users():
    from django.contrib.auth.models import User

    request = api.APIRequest(site, {
        'action': 'query',
        'list': 'allusers',
    })
    for item in request.query()['query']['allusers']:
        username = item['name'][:30]

        # TODO: how do we get their email address here? I don't think
        # it's available via the API. Maybe we'll have to fill in the
        # users' emails in a separate step.
        # We require users to have an email address, so we fill this in with a
        # dummy value for now.
        name_hash = hashlib.sha1(username).hexdigest()
        email = "%s@FIXME.localwiki.org" % name_hash

        if User.objects.filter(username=username):
            continue

        print "Importing user %s" % username
        u = User(username=username, email=email)
        u.save()


def add_redirect(page):
    global redirects

    request = api.APIRequest(site, {
        'action': 'parse',
        'title': page.title,
        'text': page.wikitext,
    })
    links = request.query()['parse']['links']
    if not links:
        return
    to_pagename = links[0]['*']

    redirects.append((page.title, to_pagename))


def process_redirects():
    # We create the Redirects here.  We don't try and port over the
    # version information for the formerly-page-text-based redirects.
    global redirects

    from pages.models import Page, slugify
    from redirects.models import Redirect

    u = get_robot_user()

    for from_pagename, to_pagename in redirects:
        try:
            to_page = Page.objects.get(slug=slugify(to_pagename))
        except Page.DoesNotExist:
            print "Error creating redirect: %s --> %s" % (
                from_pagename, to_pagename)
            print "  (page %s does not exist)" % to_pagename
            continue

        if slugify(from_pagename) == to_page.slug:
            continue
        if not Redirect.objects.filter(source=slugify(from_pagename)):
            r = Redirect(source=slugify(from_pagename), destination=to_page)
            r.save(user=u, comment="Automated edit. Creating redirect.")
            print "Redirect %s --> %s created" % (from_pagename, to_pagename)


def render_wikitext(title, s):
    """
    Attrs:
        title: Page title.
        s: MediaWiki wikitext string.

    Returns:
        HTML string of the rendered wikitext.
    """
    request = api.APIRequest(site, {
        'action': 'parse',
        'title': title,
        'text': s,
    })
    result = request.query()['parse']
    # There's a lot more in result, like page links and category
    # information.  For now, let's just grab the html text.
    return result['text']['*']


def _convert_to_string(l):
    s = ''
    for e in l:
        if isinstance(e, basestring):
            s += e
        elif isinstance(e, list):
            s += _convert_to_string(e)
        else:
            s += etree.tostring(e, encoding='UTF-8')
    return s


def _is_wiki_page_url(href):
    print SCRIPT_PATH
    if href.startswith(SCRIPT_PATH):
        return True
    else:
        split_url = urlsplit(href)
        # If this is a relative url and has 'index.php' in it we'll say
        # it's a wiki link.
        if not split_url.scheme and split_url.path.endswith('index.php'):
            return True
    return False


def _get_wiki_link(link):
    """
    If the provided link is a wiki link then we return the name of the
    page to link to.  If it's not a wiki link then we return None.
    """
    pagename = None
    if 'href' in link.attrib:
        href = link.attrib['href']
        if _is_wiki_page_url(href):
            title = link.attrib.get('title')
            if 'new' in link.attrib.get('class', '').split():
                # It's a link to a non-existent page, so we parse the
                # page name from the title attribute in a really
                # hacky way.  Titles for non-existent links look
                # like <a ... title="Page name (page does not exist)">
                pagename = title[:title.rfind('(') - 1]
            else:
                pagename = title

    return pagename


def fix_internal_links(tree):
    for elem in tree:
        for link in elem.findall('a'):
            pagename = _get_wiki_link(link)
            if pagename:
                # Set href to quoted pagename and clear out other attributes
                for k in link.attrib:
                    del link.attrib[k]
                link.attrib['href'] = urllib.quote(pagename)
    return tree


def normalize_html(html):
    """
    This is the real workhorse.  We take an html string which represents
    a rendered MediaWiki page and return cleaned up HTML.
    """
    p = html5lib.HTMLParser(tokenizer=html5lib.sanitizer.HTMLSanitizer,
            tree=html5lib.treebuilders.getTreeBuilder("lxml"),
            namespaceHTMLElements=False)
    tree = p.parseFragment(html, encoding='UTF-8')
    tree = fix_internal_links(tree)
    return _convert_to_string(tree)


def import_pages():
    from pages.models import Page, slugify

    request = api.APIRequest(site, {
        'action': 'query',
        'list': 'allpages',
    })
    print "Getting master page list.."
    response_list = request.query()['query']['allpages']
    pages = pagelist.listFromQuery(site, response_list)
    print "Got master page list."
    for mw_p in pages[:100]:
        print "Importing %s" % mw_p.title
        wikitext = mw_p.getWikiText()
        if mw_p.isRedir():
            add_redirect(mw_p)
            continue
        html = render_wikitext(mw_p.title, wikitext)

        if Page.objects.filter(slug=slugify(mw_p.title)):
            # Page already exists with this slug.  This is probably because
            # MediaWiki has case-sensitive pagenames.
            other_page = Page.objects.get(slug=slugify(mw_p.title))
            if len(html) > other_page.content:
                # *This* page has more content.  Let's use it instead.
                for other_page_version in other_page.versions.all():
                    other_page_version.delete()
                other_page.delete(track_changes=False)

        p = Page(name=mw_p.title, content=html)
        p.clean_fields()
        p.html = normalize_html(p.html)
        p.save()


def clear_out_existing_data():
    """
    A utility function that clears out existing pages, users, files,
    etc before running the import.
    """
    from pages.models import Page
    from redirects.models import Redirect

    for p in Page.objects.all():
        print 'Clearing out', p
        p.delete(track_changes=False)
        for p_h in p.versions.all():
            p_h.delete()
    for r in Redirect.objects.all():
        print 'Clearing out', r
        r.delete(track_changes=False)
        for r_h in r.versions.all():
            r_h.delete()


def run():
    clear_out_existing_data()
    import_users()
    import_pages()
    process_redirects()
