


echo "-- Installing UV and pip3"
pip3 install uv
sudo dnf install -y python3.14-pip
uv sync



echo "-- Installing Terraform"
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://rpm.releases.hashicorp.com/AmazonLinux/hashicorp.repo   
sudo dnf install -y terraform   
terraform -version  

echo "-- Bootstrap Complete --"