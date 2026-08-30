#ifndef __LOP_BMFXAPKGBlockBegin_h__
#define __LOP_BMFXAPKGBlockBegin_h__

#include <LOP/LOP_Node.h>

class LOP_BMFXAPKGBlockBegin final : public LOP_Node
{
public:
    LOP_BMFXAPKGBlockBegin(
        OP_Network *network, const char *name, OP_Operator *operator_type);
    ~LOP_BMFXAPKGBlockBegin() override;

    static PRM_Template templates[];
    static OP_Node *constructor(
        OP_Network *network, const char *name, OP_Operator *operator_type);

protected:
    OP_ERROR cookMyLop(OP_Context &context) override;

    // A negative stack effect marks this node as the beginning of the native
    // Solaris context-options block. BMFX APKG End supplies the matching
    // positive effect and owns Houdini's orange hull.
    int contextOptionsStackEffect(int input_index) const override;
};

class LOP_BMFXAPKGBlockEnd final : public LOP_Node
{
public:
    LOP_BMFXAPKGBlockEnd(
        OP_Network *network, const char *name, OP_Operator *operator_type);
    ~LOP_BMFXAPKGBlockEnd() override;

    static PRM_Template templates[];
    static OP_Node *constructor(
        OP_Network *network, const char *name, OP_Operator *operator_type);

protected:
    OP_ERROR cookMyLop(OP_Context &context) override;
    bool showConvexHull() const override;
    int contextOptionsStackEffect(int input_index) const override;
};

#endif
