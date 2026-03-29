# Watney Setup Guide for Raspberry Pi 3A+

Raspberry Pi OS (新しいバージョン) に手動でWatney環境を構築する手順。
公式Watneyイメージ (Buster Lite ベース) は古く、新しいPiでは問題が発生するためこの方法を推奨。

## 前提条件

- Raspberry Pi 3A+
- Micro SDカード (8GB以上)
- USB Ethernetアダプタ (WiFiが不安定な場合)
- PC に Raspberry Pi Imager をインストール済み

---

## 1. SDカード書き込み

Raspberry Pi Imager を使用:

- **OS**: Raspberry Pi OS Lite (32-bit)
- **歯車アイコン(設定)** で以下を事前設定:
  - ホスト名: `watney`
  - SSH を有効化 (パスワード認証)
  - ユーザー名: `pi` / パスワード: `watney5`
  - WiFi: 自宅WiFiのSSIDとパスワード
  - ロケール: タイムゾーンとキーボードレイアウト

## 2. 起動とSSH接続

SDカードをPiに挿して電源ON。1-2分待ってから:

```bash
ssh pi@watney.local
# パスワード: watney5
```

`watney.local` で繋がらない場合はルーターの管理画面やTetherアプリ等でIPを確認。

### (任意) 有線LAN接続する場合

USB Ethernetアダプタ使用時。PiとPCを直接LANケーブルで接続する場合:

**Pi側:**
```bash
sudo nmcli con mod "netplan-eth0" ipv4.method manual ipv4.addresses 192.168.1.100/24 ipv4.dns "192.168.1.1 8.8.8.8"
sudo nmcli con mod "netplan-eth0" ipv4.never-default yes
sudo nmcli con down "netplan-eth0" && sudo nmcli con up "netplan-eth0"
```

> 注: 接続名は `nmcli con show` で確認。環境により異なる場合がある。
> `ipv4.never-default yes` により eth0 がデフォルトルートにならず、WiFi経由のインターネット接続が維持される。

**PC側 (Windows):**
- イーサネットアダプタのIPv4設定:
  - IPアドレス: `192.168.1.101`
  - サブネットマスク: `255.255.255.0`
  - デフォルトゲートウェイ: 空欄

**SSH パスワード認証の有効化** (必要な場合):
```bash
sudo sed -i 's/^#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
sudo sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

### (推奨) WiFi省電力の無効化

WiFi接続が不安定な場合:
```bash
sudo iw wlan0 set power_save off
```

## 3. 初期設定

```bash
# カメラ・SPI・I2C 有効化
sudo raspi-config nonint do_camera 0
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0

# /boot/firmware/config.txt 編集 (新しいOSでは /boot/firmware/ に移動している)
sudo sed -i 's/^dtparam=audio=on/#dtparam=audio=on/g' /boot/firmware/config.txt
sudo bash -c 'cat >> /boot/firmware/config.txt << EOF
disable_camera_led=1
dtoverlay=googlevoicehat-soundcard
dtoverlay=i2s-mmap
gpio=13=op,dl
gpio=25=op,dl
gpio=24=op,dl
gpio=17=op,dl
gpio=27=op,dl
EOF'
```

## 4. Watney本体のインストール

```bash
# システムパッケージ (python-smbus → python3-smbus に変更)
sudo apt-get -y update && sudo apt-get -y upgrade
sudo apt-get -y install git python3-pip pigpio python3-pigpio python3-rpi.gpio python3-smbus python3-numpy

# pigpioデーモン有効化
sudo systemctl enable pigpiod
sudo sed -i 's:^ExecStart=/usr/bin/pigpiod -l:ExecStart=/usr/bin/pigpiod -l -t 0:g' /lib/systemd/system/pigpiod.service

# Pythonパッケージ (--break-system-packages が必要)
pip3 install --break-system-packages aiohttp apa102-pi psutil pyalsaaudio smbus

# Watneyリポジトリのクローン
cd /home/pi
git clone https://github.com/obikata/watney.git
cd /home/pi/watney
git checkout feature/bookworm-support
cd /home/pi
cp /home/pi/watney/key.pem /home/pi
cp /home/pi/watney/cert.pem /home/pi

# サービス登録
sudo cp /home/pi/watney/packer/watney.service /etc/systemd/system/
sudo systemctl enable watney
```

## 5. スワップ追加

Pi 3A+ は RAM 512MB のため、Janus のビルドでメモリ不足になる。事前にスワップを追加:

```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# 確認
free -h
```

永続化する場合:
```bash
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 6. TTS インストール

オリジナルの Watney は mimic1 を使用しているが、新しいOSのGCCではビルドに失敗する。
代わりに espeak を使用する (apt一発でインストール可能):

```bash
sudo apt-get -y install espeak
```

> 注: feature/bookworm-support ブランチでは rover.conf の TTSCommand が既に espeak に変更済み。
> masterブランチを使う場合は手動で変更が必要:
> `sed -i 's|TTSCommand=mimic -voice slt --setf int_f0_target_mean=90 -t {}|TTSCommand=espeak -v en "{}"|' /home/pi/watney/rover.conf`

## 7. Janus WebRTCサーバー ビルド・インストール

ビルドに時間がかかるため screen の使用を推奨:
```bash
sudo apt-get -y install screen
screen -S janus
```

screen の中で:

```bash
# 依存パッケージ
sudo apt-get -y install libmicrohttpd-dev libjansson-dev libssl-dev libsrtp-dev libsofia-sip-ua-dev libglib2.0-dev libopus-dev libogg-dev libcurl4-openssl-dev liblua5.3-dev libconfig-dev pkg-config gengetopt libtool automake libnice-dev

# libsrtp
cd /home/pi
wget https://github.com/cisco/libsrtp/archive/v2.3.0.tar.gz
tar xfv v2.3.0.tar.gz
cd libsrtp-2.3.0
./configure --prefix=/usr --enable-openssl
make shared_library && sudo make install
cd ..
rm -rf libsrtp-2.3.0 v2.3.0.tar.gz

# Janus Gateway
git clone https://github.com/meetecho/janus-gateway.git
cd janus-gateway
git checkout 5ec8568709c483ae89b1aa77e127d14c3b59428c
sh autogen.sh
./configure --prefix=/opt/janus --disable-aes-gcm
make
sudo make install
cd ..
rm -rf janus-gateway

# Janus設定ファイル配置
sudo cp -r /home/pi/watney/janus/* /opt/janus/etc/janus/
sudo chown -R pi /opt/janus
```

> screen のデタッチ: `Ctrl+A` → `D` / 再接続: `screen -r janus`

## 8. GStreamer インストール

```bash
sudo apt-get -y install gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-alsa python3-gst-1.0
```

## 9. ALSA設定

```bash
sudo cp /home/pi/watney/packer/asound.conf /etc/asound.conf
sudo cp /home/pi/watney/packer/asound.state /var/lib/alsa/asound.state
sudo chown root /etc/asound.conf
sudo chmod u=rw,g=r,o=r /etc/asound.conf
sudo chown root /var/lib/alsa/asound.state
sudo chmod u=rw,g=r,o=r /var/lib/alsa/asound.state
```

## 10. 再起動・動作確認

```bash
sudo reboot
```

再起動後、ブラウザで `https://<PiのIP>:5000` にアクセスして動作確認。

---

## 新しいOS対応で変更した点まとめ

| 項目 | 旧 (Buster) | 新 |
|---|---|---|
| config.txt の場所 | `/boot/config.txt` | `/boot/firmware/config.txt` |
| パッケージ名 | `python-smbus` | `python3-smbus` |
| pip install | そのまま実行可 | `--break-system-packages` が必要 |
| ネットワーク管理 | `dhcpcd` | `NetworkManager (nmcli)` |
| スワップ設定 | `dphys-swapfile` | 手動で `/swapfile` 作成 |
| TTS | mimic1 (ソースビルド) | espeak (aptインストール) に変更 |
| 映像配信 | `raspivid` | `libcamera-vid` に変更 |
| SSL | `ssl.create_default_context()` | `ssl.SSLContext(PROTOCOL_TLS_SERVER)` に変更 |
| PowerPlant | I2C未接続でクラッシュ | I2C未接続時はスキップ |
