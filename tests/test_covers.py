"""Cover screening, small previews and the cover list, on fictitious archives made here."""
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image, ImageDraw

from manga_kit import create_app
from manga_kit.config import Settings
from manga_kit.covers import COLOR_LIMIT, MONO_LIMIT, colourfulness, screen
from manga_kit.filesystem import LibraryFilesystem

GREY, RED = (128, 128, 128), (200, 30, 40)


def page(colour, size=(120, 180)):
    output = io.BytesIO()
    Image.new('RGB', size, colour).save(output, 'JPEG')
    return output.getvalue()


def write_book(path, pages):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        for index, data in enumerate(pages, 1):
            archive.writestr(f'{index:04}.jpg', data)


class CoverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        library = self.root / 'library' / '連載中'
        self.books = {'swap': (library / '[架空作者] 入れ替え' / '入れ替え 第01巻.cbz', [page(GREY), page(RED)]),
                      'mono': (library / '[架空作者] 白黒' / '白黒 第01巻.cbz', [page(GREY), page(GREY)]),
                      'color': (library / '[架空作者] カラー' / 'カラー 第01巻.cbz', [page(RED, (600, 900)), page(GREY)]),
                      'empty': (library / '[架空作者] 画像なし' / '画像なし 第01巻.cbz', [])}
        for path, pages in self.books.values():
            write_book(path, pages)
        broken = library / '[架空作者] 破損' / '破損 第01巻.cbz'
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b'not an archive')
        self.books['broken'] = (broken, None)
        env = patch.dict(os.environ, {}, clear=True); env.start(); self.addCleanup(env.stop)
        self.settings = Settings(demo=False, library_root=self.root / 'library', data_root=self.root / 'state')
        self.app = create_app(settings=self.settings); self.app.testing = True
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.client = self.app.test_client()
        self.ids = {key: next(book['id'] for row in self.app.extensions['inventory'].cached()['series'] for book in row['books']
                              if book['path'] == path.relative_to(self.root / 'library').as_posix())
                    for key, (path, _) in self.books.items()}

    def wait(self, response):
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        for _ in range(400):
            job = self.app.extensions['state'].job(response.json['id'], include_result=True)
            if job['status'] not in {'queued', 'running'}:
                return job
            time.sleep(.05)
        self.fail('job did not finish')

    def test_colourfulness_separates_grey_pale_and_colour_pages_and_screen_names_each_group(self):
        self.assertLess(colourfulness(page(GREY)), MONO_LIMIT)
        self.assertGreater(colourfulness(page(RED)), COLOR_LIMIT)
        # A mostly white cover with a little colour is still a colour cover.
        pale = Image.new('RGB', (300, 450), (250, 248, 245)); draw = ImageDraw.Draw(pale)
        draw.ellipse((100, 80, 200, 200), fill=(240, 205, 185)); draw.rectangle((90, 60, 210, 100), fill=(150, 120, 100))
        for y in range(250, 430, 20):
            draw.line((40, y, 260, y), fill=(30, 30, 30), width=2)
        output = io.BytesIO(); pale.save(output, 'JPEG')
        self.assertGreater(colourfulness(output.getvalue()), COLOR_LIMIT)
        expected = {'swap': 'mono_color', 'mono': 'mono_first', 'color': 'ok', 'empty': 'broken', 'broken': 'broken'}
        with LibraryFilesystem(self.root / 'library') as fs:
            for key, bucket in expected.items():
                with self.subTest(book=key):
                    path = self.books[key][0].relative_to(self.root / 'library').as_posix()
                    self.assertEqual(screen(fs, path)['bucket'], bucket)

    def test_screening_is_incremental_and_groups_the_cover_list_by_state(self):
        job = self.wait(self.client.post('/api/tools/covers/screen', json={}))
        self.assertEqual(job['status'], 'succeeded', job.get('error'))
        self.assertEqual(job['result']['counts'], {'mono_color': 1, 'mono_first': 1, 'broken': 2})
        self.assertEqual(self.wait(self.client.post('/api/tools/covers/screen', json={}))['result']['checked'], 0)
        # Verdicts from an earlier measure are judged again even without force.
        store = self.app.extensions['state']
        store.cache('covers:screening', {**store.cached('covers:screening')['data'], 'method': 'colour-share'})
        self.assertEqual(self.wait(self.client.post('/api/tools/covers/screen', json={}))['result']['checked'], 5)
        self.assertEqual(self.wait(self.client.post('/api/tools/covers/screen', json={'force': True}))['result']['checked'], 5)
        html = self.client.get('/pages').get_data(as_text=True)
        self.assertIn('1枚目が白黒 <span class="count">1</span>', html)
        self.assertIn('入れ替え', html)
        self.assertIn('/api/tools/books/%s/page/2/preview' % self.ids['swap'], html)
        self.assertNotIn('白黒 第01巻', html.split('<tbody>')[1])
        broken = self.client.get('/pages?bucket=broken').get_data(as_text=True).split('<tbody>')[1]
        self.assertEqual(broken.count('<tr'), 2)
        self.assertIn('カラー', self.client.get('/pages?bucket=lib&q=カラー').get_data(as_text=True).split('<tbody>')[1])
        self.assertEqual(self.client.get('/pages?bucket=unknown').status_code, 400)
        self.assertEqual(self.client.post('/api/tools/pages/%s/mark' % self.ids['swap'], json={'status': 'done'}).status_code, 200)
        self.assertNotIn('<tbody>', self.client.get('/pages?bucket=mono_color').get_data(as_text=True))
        self.assertIn('入れ替え', self.client.get('/pages?bucket=mono_color&status=done').get_data(as_text=True))

    def test_previews_and_thumbnails_are_small_cached_jpegs(self):
        for url, width in (('/api/tools/books/%s/page/1/preview' % self.ids['color'], 240),
                           ('/api/tools/books/%s/thumbnail' % self.ids['color'], 360)):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual((response.status_code, response.mimetype), (200, 'image/jpeg'))
                self.assertEqual(response.headers['Cache-Control'], 'private, max-age=86400')
                with Image.open(io.BytesIO(response.data)) as image:
                    self.assertLessEqual(image.width, width)

    def test_page_editor_returns_only_to_the_cover_list(self):
        book = self.ids['color']
        html = self.client.get('/pages/%s?back=https://evil.example/' % book).get_data(as_text=True)
        self.assertIn('class="back-link" href="/pages"', html)
        self.assertNotIn('evil.example', html)
        html = self.client.get('/pages/%s?back=/pages?bucket%%3Dbroken' % book).get_data(as_text=True)
        self.assertIn('class="back-link" href="/pages?bucket=broken"', html)
        self.assertIn('/api/tools/books/%s/page/2/preview' % book, html)


if __name__ == '__main__':
    unittest.main()
