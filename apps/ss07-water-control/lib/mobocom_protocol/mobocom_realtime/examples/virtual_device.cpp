#include "virtual_driver.hpp"
#include <iostream>
int main() {
    using namespace mobocom;
    auto profile=examples::profile(1); // virtual lens; 2=gimbal, 3=fans
    examples::VirtualDriver driver;
    DeviceCore node(profile,1,10,driver.hooks());
    if(!node.valid())return 1;
    if(node.select_local_source(Source::Remote,node.session_epoch(),node.generation(),100000,300000,0)!=Error::Ok)return 2;
    node.tick(0); // independently scheduled owner/watchdog task
    LocalControlInput input(node);
    const Target targets[]={{1,1,100},{1,2,200},{1,3,300}};
    if(input.targets(node.session_epoch(),node.generation(),1,targets,3,1000)!=Error::Ok)return 3;
    node.tick(1000);
    std::cout<<"Virtual lens target handed to driver; fixed core bytes="<<sizeof(node)<<'\n';
}
