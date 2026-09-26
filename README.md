# Akdeniz yayın sistemi

Bu depo, `playlist.txt` içindeki videoları sırayla FFmpeg ile işler ve Render üzerinde sabit `stream.m3u8` adresinden sunar.

## Dosya düzeni

Videoları ve iki PNG görselini GitHub deposundaki `media/` klasörüne koyun:

```text
media/
  yayin.png
  reklam.png
  video1.mp4
  reklam.mp4
  video2.mp4
playlist.txt
```

`playlist.txt` içine her satıra bir video dosya adı yazın. Sıra aynen takip edilir. Boş satırlar ve `#` ile başlayan yorumlar yok sayılır. Örnek listedeki adları kendi videolarınızla değiştirin.

- Dosya adı tam olarak `reklam.mp4` olan videoda `media/reklam.png` kullanılır.
- Diğer bütün videolarda `media/yayin.png` kullanılır.
- PNG overlay ekranı tamamen kaplar. Görsel 16:9 değilse 960×540'a esnetilir.
- Video 960×540 (540p) H.264, ses AAC olarak kodlanır. Ses parçası olmayan videolar da desteklenir.
- Liste sona erince yayın baştan döner.

## Render kurulumu

1. Bu dosyaları GitHub deposuna gönderin; `media/` içine videoları ve logoları ekleyin, `playlist.txt` listesini düzenleyin.
2. Render Dashboard'da **New + → Web service** ile bu depoyu seçin. `render.yaml`, Docker web servisini tanımlar.
3. İlk dağıtımın tamamlanmasını bekleyin. Servis adresiniz `https://<servis-adı>.onrender.com` biçiminde olur.
4. Yayın URL'si: `https://<servis-adı>.onrender.com/stream.m3u8`. Kök adres `/` de bu URL'ye yönlenir.

Render'ın geçici dosya sistemi yeniden dağıtımda sıfırlanabilir; bu kurulumda videolar repodaki `media/` klasöründen gelir ve HLS segmentleri belleğe alınabilir geçici diske yazılır. Uzun süreli, kesintisiz yayın için Render'ın ücretli/uygun bir servisini ve gerekiyorsa kalıcı diskini seçin. Depoya büyük videolar koyacaksanız GitHub dosya boyutu sınırlarını dikkate alın; gerekirse depolama yolunu harici kalıcı depoya uyarlayın.

## Yerelde çalıştırma

Docker ile:

```sh
docker build -t m3u8-yayin .
docker run --rm -p 10000:10000 -v "$(pwd)/media:/app/media:ro" -v "$(pwd)/playlist.txt:/app/playlist.txt:ro" m3u8-yayin
```

Ardından `http://localhost:10000/stream.m3u8` adresini açın. `media/` klasöründe `reklam.png` ve `yayin.png` bulunmalıdır.

## Notlar

- Playlist'e yalnızca `media/` içindeki göreli dosya adlarını yazın; alt klasörler desteklenir. Mutlak yollar ve `..` geçişleri reddedilir.
- Video veya logo eksikse ilgili kayıt loglanır; video atlanır. Logları Render servisindeki **Logs** bölümünden izleyin.
- Her videonun HLS segmentleri, sonraki videoya geçilirken aynı `stream.m3u8` listesine eklenir; liste sonlu bir pencere tutar.
