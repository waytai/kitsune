from os.path import basename
from urlparse import urlparse, parse_qs

from django.conf import settings

import jingo
from tower import ugettext_lazy as _lazy, ugettext as _
from wikimarkup.parser import Parser

from gallery.models import Image, Video
from sumo import email_utils
from sumo.urlresolvers import reverse


ALLOWED_ATTRIBUTES = {
    'a': ['href', 'title', 'class', 'rel'],
    'div': ['id', 'class', 'style', 'data-for', 'title', 'data-target',
            'data-modal'],
    'h1': ['id'],
    'h2': ['id'],
    'h3': ['id'],
    'h4': ['id'],
    'h5': ['id'],
    'h6': ['id'],
    'li': ['class'],
    'span': ['class', 'data-for'],
    'img': ['class', 'src', 'data-original-src', 'alt', 'title', 'height',
            'width', 'style'],
    'video': ['height', 'width', 'controls', 'data-fallback', 'poster',
              'data-width', 'data-height'],
    'source': ['src', 'type'],
}
ALLOWED_STYLES = ['vertical-align']
IMAGE_PARAMS = ['alt', 'align', 'caption', 'valign', 'frame', 'page', 'link',
                'width', 'height']
IMAGE_PARAM_VALUES = {
    'align': ('none', 'left', 'center', 'right'),
    'valign': ('baseline', 'sub', 'super', 'top', 'text-top', 'middle',
               'bottom', 'text-bottom'),
}
VIDEO_PARAMS = ['height', 'width', 'modal', 'title', 'placeholder']
YOUTUBE_PLACEHOLDER = 'YOUTUBE_EMBED_PLACEHOLDER_%s'


def wiki_to_html(wiki_markup, locale=settings.WIKI_DEFAULT_LANGUAGE,
                 nofollow=True):
    """Wiki Markup -> HTML"""
    return WikiParser().parse(wiki_markup, show_toc=False, locale=locale,
                              nofollow=nofollow)


def get_object_fallback(cls, title, locale, default=None, **kwargs):
    """Return an instance of cls matching title and locale, or fall
    back to the default locale.

    When falling back to the default locale, follow any wiki redirects
    internally.

    If the fallback fails, the return value is `default`.

    You may pass in additional kwargs which go straight to the query.

    """
    try:
        return cls.objects.get(title=title, locale=locale, **kwargs)
    except cls.DoesNotExist:
        pass

    # Fallback
    try:
        default_lang_doc = cls.objects.get(
            title=title, locale=settings.WIKI_DEFAULT_LANGUAGE, **kwargs)

        # Return the translation of this English item:
        if hasattr(default_lang_doc, 'translated_to'):
            trans = default_lang_doc.translated_to(locale)
            if trans and trans.current_revision:
                return trans

        # Follow redirects internally in an attempt to find a
        # translation of the final redirect target in the requested
        # locale. This happens a lot when an English article is
        # renamed and a redirect is left in its wake: we wouldn't want
        # the non-English user to be linked to the English redirect,
        # which would happily redirect them to the English final
        # article.
        if hasattr(default_lang_doc, 'redirect_document'):
            target = default_lang_doc.redirect_document()
            if target:
                trans = target.translated_to(locale)
                if trans and trans.current_revision:
                    return trans

        # Return the English item:
        return default_lang_doc
    # Okay, all else failed
    except cls.DoesNotExist:
        return default


def _get_wiki_link(title, locale):
    """Checks the page exists, and returns its URL or the URL to create it.

    Return value is a dict: {'found': boolean, 'url': string}.
    found is False if the document does not exist.

    """
    # Prevent circular import. sumo is conceptually a utils apps and
    # shouldn't have import-time (or really, any, but that's not going
    # to happen) dependencies on client apps.
    from wiki.models import Document

    d = get_object_fallback(Document, locale=locale, title=title,
                            is_template=False)
    if d:
        # If the article redirects use its destination article
        while d.redirect_document():
            d = d.redirect_document()

        # The locale in the link urls should always match the current
        # document's locale even if the document/slug being linked to
        # is in the default locale.
        url = reverse('wiki.document', locale=locale, args=[d.slug])
        return {'found': True, 'url': url, 'text': d.title}

    # To avoid circular imports, wiki.models imports wiki_to_html
    from sumo.helpers import urlparams
    return {'found': False,
            'text': title,
            'url': urlparams(reverse('wiki.new_document', locale=locale),
                             title=title)}


def build_hook_params(string, locale, allowed_params=[],
                      allowed_param_values={}):
    """Parses a string of the form 'some-title|opt1|opt2=arg2|opt3...'

    Builds a list of items and returns relevant parameters in a dict.

    """
    if not '|' in string:  # No params? Simple and easy.
        string = string.strip()
        return (string, {'alt': string})

    items = [i.strip() for i in string.split('|')]
    title = items.pop(0)
    params = {}

    last_item = ''
    for item in items:  # this splits by = or assigns the dict key to True
        if '=' in item:
            param, value = item.split('=', 1)
            params[param] = value
        else:
            params[item] = True
            last_item = item

    if 'caption' in allowed_params:
        params['caption'] = title
        # Allowed parameters are not caption. All else is.
        if last_item and last_item not in allowed_params:
            params['caption'] = items.pop()
            del params[last_item]
        elif last_item == 'caption':
            params['caption'] = last_item

    # Validate params allowed
    for p in params.keys():
        if p not in allowed_params:
            del params[p]

    # Validate params with limited # of values
    for p in allowed_param_values:
        if p in params and params[p] not in allowed_param_values[p]:
            del params[p]

    # Handle page as a special case
    if 'page' in params and params['page'] is not True:
        link = _get_wiki_link(params['page'], locale)
        params['link'] = link['url']
        params['found'] = link['found']

    return (title, params)


class WikiParser(Parser):
    """Wrapper for wikimarkup which adds Kitsune-specific callbacks
    and setup.
    """

    def __init__(self, base_url=None):
        super(WikiParser, self).__init__(base_url)

        # Register default hooks
        self.registerInternalLinkHook(None, self._hook_internal_link)
        self.registerInternalLinkHook('Image', self._hook_image_tag)
        self.registerInternalLinkHook('Video', self._hook_video)
        self.registerInternalLinkHook('V', self._hook_video)

        self.youtube_videos = []

    def parse(self, text, show_toc=None, tags=None, attributes=None,
              styles=None, locale=settings.WIKI_DEFAULT_LANGUAGE,
              nofollow=False, youtube_embeds=True):
        """Given wiki markup, return HTML.

        Pass a locale to get all the hooks to look up Documents or
        Media (Video, Image) for that locale. We key Documents by
        title and locale, so both are required to identify it for a
        e.g. link.

        Since py-wikimarkup's hooks don't offer custom paramters for
        callbacks, we're using self.locale to keep things simple.
        """
        self.locale = locale

        parser_kwargs = {'tags': tags} if tags else {}

        @email_utils.safe_translation
        def _parse(locale):
            return super(WikiParser, self).parse(
                text,
                show_toc=show_toc,
                attributes=attributes or ALLOWED_ATTRIBUTES,
                styles=styles or ALLOWED_STYLES,
                nofollow=nofollow,
                strip_comments=True,
                **parser_kwargs)

        html = _parse(locale)

        # This is kind of a hack so that subclasses can skip embedding here
        # and do it on their own at the end of parsing.
        if youtube_embeds:
            html = self.add_youtube_embeds(html)

        return html

    def add_youtube_embeds(self, html):
        """Insert youtube embeds.

        We need to play this placeholder replacement game because we don't
        allow iframes in the rendered content.
        """
        for video_id in self.youtube_videos:
            html = html.replace(YOUTUBE_PLACEHOLDER % video_id,
                                generate_youtube_embed(video_id))
        return html

    def _hook_internal_link(self, parser, space, name):
        """Parses text and returns internal link."""
        text = False
        title = name

        # Split on pipe -- [[href|name]]
        if '|' in name:
            title, text = title.split('|', 1)

        hash = ''
        if '#' in title:
            title, hash = title.split('#', 1)

        # Sections use _, page names use +
        if hash != '':
            hash = '#' + hash.replace(' ', '_')

        # Links to this page can just contain href="#hash"
        if title == '' and hash != '':
            if not text:
                text = hash.replace('_', ' ')
            return u'<a href="%s">%s</a>' % (hash, text)

        link = _get_wiki_link(title, self.locale)
        extra_a_attr = ''
        if not link['found']:
            extra_a_attr += (u' class="new" title="{tooltip}"'
                             .format(tooltip=_('Page does not exist.')))
        if not text:
            text = link['text']
        return u'<a href="{url}{hash}"{extra}>{text}</a>'.format(
            url=link['url'], hash=hash, extra=extra_a_attr, text=text)

    def _hook_image_tag(self, parser, space, name):
        """Adds syntax for inserting images."""
        title, params = build_hook_params(name, self.locale, IMAGE_PARAMS,
                                          IMAGE_PARAM_VALUES)

        message = _lazy(u'The image "%s" does not exist.') % title
        image = get_object_fallback(Image, title, self.locale, message)
        if isinstance(image, basestring):
            return image

        template = jingo.env.get_template('wikiparser/hook_image.html')
        r_kwargs = {'image': image, 'params': params,
                    'MEDIA_URL': settings.MEDIA_URL}
        return template.render(r_kwargs)

    # Videos are objects that can have one or more files attached to them
    #
    # They are keyed by title in the syntax and the locale passed to the
    # parser.
    def _hook_video(self, parser, space, title):
        """Handles [[Video:video title]] with locale from parser."""
        message = _lazy(u'The video "%s" does not exist.') % title

        # params, only modal supported for now
        title, params = build_hook_params(title, self.locale, VIDEO_PARAMS)

        # If this is a youtube video, return the youtube embed placeholder
        parsed_url = urlparse(title)
        netloc = parsed_url.netloc
        if netloc in ['youtu.be', 'youtube.com', 'www.youtube.com']:
            if netloc == 'youtu.be':
                # The video id is the path minus the leading /
                video_id = parsed_url.path[1:]
            else:
                # The video id is in the v= query param
                video_id = parse_qs(parsed_url.query)['v'][0]

            self.youtube_videos.append(video_id)

            return YOUTUBE_PLACEHOLDER % video_id

        v = get_object_fallback(Video, title, self.locale, message)
        if isinstance(v, basestring):
            return v

        return generate_video(v, params)


def generate_video(v, params=[]):
    """Takes a video object and returns HTML markup for embedding it."""
    sources = []
    if v.webm:
        sources.append({'src': _get_video_url(v.webm), 'type': 'webm'})
    if v.ogv:
        sources.append({'src': _get_video_url(v.ogv), 'type': 'ogg'})
    data_fallback = ''
    # Flash fallback
    if v.flv:
        data_fallback = _get_video_url(v.flv)
    return jingo.env.get_template('wikiparser/hook_video.html').render(
        {'fallback': data_fallback, 'sources': sources, 'params': params,
         'video': v,
         'height': settings.WIKI_VIDEO_HEIGHT,
         'width': settings.WIKI_VIDEO_WIDTH})


def generate_youtube_embed(video_id):
    """Takes a youtube video id and returns the embed markup."""
    return jingo.env.get_template(
        'wikiparser/hook_youtube_embed.html').render({'video_id': video_id})


def _get_video_url(video_file):
    if settings.GALLERY_VIDEO_URL:
        return settings.GALLERY_VIDEO_URL + basename(video_file.name)
    return video_file.url
