set -e

sudo apt-get install -y openjdk-7-jre-headless
wget -qO - https://packages.elastic.co/GPG-KEY-elasticsearch | sudo apt-key add -
echo "deb http://packages.elastic.co/elasticsearch/2.x/debian stable main" | sudo tee -a /etc/apt/sources.list.d/elasticsearch-2.x.list
sudo add-apt-repository ppa:chris-lea/node.js -y
sudo apt-get update
sudo apt-get install elasticsearch  -y
sudo apt-get install  nodejs -y

sudo update-rc.d elasticsearch defaults 95 10
sudo service elasticsearch start

sudo apt-get install --reinstall make -y
sudo apt-get install --reinstall make -y
sudo apt-get install -y  ruby gem ruby-dev unzip fabric git apache2 libapache2-mod-proxy-html  libxml2-dev supervisor  tcl8.5 software-properties-common python-software-properties 


if [ "$USER" != "travis" ]
    then
    
    sudo service redis-server start
    sudo a2enmod proxy proxy_http headers expires rewrite
    sudo gem install compass
    sudo npm install -g bower grunt-cli coffee-script
    sudo add-apt-repository ppa:chris-lea/redis-server -y
    sudo apt-get update
    sudo apt-get install -y redis-server
    sudo update-rc.d redis-server defaults
fi

sudo service redis-server start
