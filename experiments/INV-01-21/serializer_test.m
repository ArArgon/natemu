/* Executes only the real serialization function on fabricated rows. All native
 * sampling/notification/timer/lifecycle entry points remain uncalled. */
static void serializer_pause(void);
#define M21_SERIALIZE_HOOK serializer_pause
#define main unused_native_entry
#include "producer.m"
#undef main
static bool paused;
static void serializer_pause(void){if(paused){puts("prefix-ready");fflush(stdout);char c;require(read(STDIN_FILENO,&c,1)==1,"fixture resume");}}
int main(int argc,char **argv){
    require(argc==2,"fixture mode");workers=1;total=10;arm="C2";run_id="00000000000000000000000000000000";tb.numer=tb.denom=1;
    for(unsigned k=0;k<5;k++)resolutions[k]=1;
    for(unsigned i=0;i<total;i++){
        actors[i].rows=calloc(2,sizeof(m21_record));require(actors[i].rows!=NULL,"fixture rows");
        for(unsigned j=0;j<2;j++)atomic_init(&actors[i].rows[j].committed,0);
        if(i>1)continue;atomic_store(&actors[i].reserved,2);
        actors[i].continuous.reads=i==1?7:4;
        for(unsigned j=0;j<2;j++){
            m21_record *r=&actors[i].rows[j];r->seq=j;r->counter=i==1?j+1:j;r->actor=i;r->thread_id=i+1;r->requested=r->got=25;r->source=i==1?-2:-1;
            for(unsigned k=0;k<5;k++){r->clocks[k]=UINT64_C(1000000000)+j*UINT64_C(400000000);m21_observe(&actors[i].clocks[k],r->clocks[k],j);}
            atomic_store_explicit(&r->committed,1,memory_order_release);
        }
    }
    if(!strcmp(argv[1],"before")){paused=true;serializer_pause();}
    paused=!strcmp(argv[1],"during");serialize();return 0;
}
